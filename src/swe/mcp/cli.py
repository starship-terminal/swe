#!/usr/bin/env python3
"""swe mcp — MCP server manager for AI coding tools.

Usage: swe mcp <command> [args] [--help]

Commands:
  discover [--apply] [--json]     Find MCP servers across tool configs
  list [tool]                    Matrix view of MCP servers across tools
  status [tool]                  List with health checks
  sync <source> <target...>      Copy servers source → target(s) (any pair)
  diff <tool1> <tool2>           Compare two tools' configs
  edit <tool> <name>             Edit one server config in one tool
  validate [tool...]             Validate MCP config shape for one/all tools
  add <name> ...                 Add a server to the hub (and harnesses)
  remove <name> [--from=...]     Remove a server everywhere, or from given places
  enable/disable <name>          Switch a server on/off per harness
  set <name> ...                 Change a hub server's URL, command or keys
  rename <old> <new>             Rename a server everywhere
  set-type <name> TYPE           Label a hub server
  info <name>                    One server's full detail
  tools <name>                   List a server's tools by handshake
  doctor                         Deep diagnostics
  gateway                        Shared-server flags + the daemon's status
  secret set NAME                Store a ${NAME} value in the secrets file

Run 'swe mcp <command> help' for detailed help on each command.
"""

import fnmatch
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path

from swe.configuration import (
    ConfigurationError,
    CorruptConfigurationError,
    parse_json_object,
)
from swe.console import c, cpad, sanitize, strip_ansi, terminal_width, truncate, visible_len
from swe.json_output import emit, take_json_flag, error_stream
from swe.table import Table
from swe.harness.registry import load_registry as _load_registry
from swe.harness.registry import alias_map as _harness_alias_map
from swe.mcp.inventory import server_inventory
from swe.mcp import gateway as gateway_defaults
from swe.flags import expand_value_flags
from swe.mcp.models import ToggleReply
from swe.mcp.models import InfoReply, PlaceRow, SetTypeReply, ToolsReply
from swe.mcp.secrets import _SECRET_FLAG_RE
from swe.mcp.secrets import load_secrets
from swe.mcp.secrets import redact as redact_secrets
from swe.mcp.secrets import shield_credentials
from swe.mcp.secrets import resolve as resolve_secrets
from swe.mcp.secrets import scrub_args, scrub_url
from swe.mcp.secrets import remove_secret, set_secret
from swe.mcp.secrets import unresolved_names
from swe.paths import atomic_write_text, CONFIG_DIR, MCP_SOURCE_FILE
from swe.mcp.formats import (
    McpFormatHandler,
    convert_server_between_formats,
    get_conversion_issues,
    get_format_handler,
    normalize_server as normalize_server_any,
)
from swe.mcp.codex_io import load_codex_servers, save_codex_servers
from swe.multiselect import Choice, multiselect


# Maps canonical tool name → MCP config location.
# Tools not listed here are skipped by mcp commands.
MCP_CONFIG_MAP = {
    "claude": {
        "path": Path.home() / ".claude.json",
        "key": "mcpServers",
        "label": "Claude Code",
        "format": "claude",
    },
    "claude-desktop": {
        "path": Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",
        "key": "mcpServers",
        "label": "Claude Desktop",
        # Its config file launches local servers only; a remote one is added
        # in Claude Desktop itself (Settings > Connectors).
        "local_only": True,
    },
    "cursor": {
        "path": Path.home() / ".cursor" / "mcp.json",
        "key": "mcpServers",
        "label": "Cursor",
    },
    "copilot": {
        "path": Path.home() / ".copilot" / "mcp-config.json",
        "key": "mcpServers",
        "label": "Copilot",
        "format": "copilot",
    },
    "cline": {
        "path": Path.home() / ".cline" / "data" / "settings" / "cline_mcp_settings.json",
        "key": "mcpServers",
        "label": "Cline",
    },
    "lmstudio": {
        "path": Path.home() / ".lmstudio" / "mcp.json",
        "key": "mcpServers",
        "label": "LM Studio",
    },
    "gemini": {
        "path": Path.home() / ".gemini" / "antigravity" / "mcp_config.json",
        "key": "mcpServers",
        "label": "Gemini",
    },
    "opencode": {
        "path": Path.home() / ".config" / "opencode" / "opencode.json",
        "key": "mcp",
        "label": "opencode",
        "format": "opencode",
    },
    "kilo": {
        # Kilo is an opencode fork: same mcp schema in its own config.
        "path": Path.home() / ".config" / "kilo" / "kilo.jsonc",
        "key": "mcp",
        "label": "Kilo",
        "format": "opencode",
    },
    "codex": {
        "path": Path.home() / ".codex" / "config.toml",
        "key": "mcp_servers",
        "label": "Codex",
        "format": "standard",
        "io_type": "toml_region",
    },
    "droid": {
        "path": Path.home() / ".factory" / "mcp.json",
        "key": "mcpServers",
        "label": "Factory Droid",
        "format": "droid",
    },
    # Devin Local 3.6+ (CLI v3000.3): dedicated mcp_config.json.
    # Docs: https://docs.devin.ai/cli/extensibility/mcp/configuration
    # Not ~/.devin/mcp.json (the unverified ~/.<tool>/mcp.json guess).
    "devin": {
        "path": Path.home() / ".config" / "devin" / "mcp_config.json",
        "key": "mcpServers",
        "label": "Devin",
        "format": "standard",
    },
}

# Aliases for tools that aren't in the harness.json registry (always-available peers).
_EXTRA_ALIASES = {
    "claude-desktop": "claude-desktop",
    "cd": "claude-desktop",
    "codex": "codex",
    "cx-toml": "codex",
}


def _unverified_mcp_config(tool_name: str) -> dict:
    """Optimistic default MCP descriptor for a harness without an explicit
    ``MCP_CONFIG_MAP`` entry.

    Assumes the de-facto convention ``~/.<tool>/mcp.json`` with the
    standard ``mcpServers`` key. Flagged ``unverified`` so the UI can
    distinguish "we know this path is correct" from "we're guessing".
    Callers should gate input via :func:`resolve_tool_arg` so typo'd
    names don't silently get a default.
    """
    return {
        "path": Path.home() / f".{tool_name}" / "mcp.json",
        "key": "mcpServers",
        "label": "unverified",
        "format": "standard",
        "unverified": True,
    }


# Registry names become ``~/.<name>/mcp.json``. A hand-edited or malformed
# registry entry containing ``/``, ``\`` or ``..`` would turn `mcp sync`
# into a write outside the home directory — only plain names are safe.
_SAFE_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def get_tool_config(tool_name: str) -> dict | None:
    """Return the MCP config descriptor for any tool name, or None.

    Verified tools use ``MCP_CONFIG_MAP``. Any other non-empty name gets
    an optimistic unverified default (see :func:`_unverified_mcp_config`),
    unless the name could not form a safe ``~/.<name>/`` path.
    """
    if not tool_name:
        return None
    if is_hub(tool_name):
        return {"path": MCP_SOURCE_FILE, "key": MCP_SOURCE_KEY,
                "label": "hub", "format": "standard"}
    cfg = MCP_CONFIG_MAP.get(tool_name)
    if cfg:
        return cfg
    if not _SAFE_TOOL_NAME_RE.match(tool_name):
        return None
    return _unverified_mcp_config(tool_name)


# ── Registry & alias resolution ─────────────────────────────────────


def load_registry() -> dict:
    return _load_registry()


def alias_map(registry: dict) -> dict:
    """Return {alias_or_name: canonical_name} for every tool (+ MCP extras)."""
    amap = _harness_alias_map(registry)
    amap.update(_EXTRA_ALIASES)
    return amap


def resolve(registry: dict, key: str) -> str | None:
    return alias_map(registry).get(key)


def _unsafe_registry_name(name: str | None) -> bool:
    """True when an arg resolved to a name ``get_tool_config`` refuses.

    resolve_tool_arg accepts every registry name, but a name that cannot
    form a safe ``~/.<name>/`` path gets no config and is skipped by
    get_mcp_tools — indexing its output with such a name would raise
    KeyError. The hub resolves to its own label and is always usable.
    """
    return bool(name) and not is_hub(name) and get_tool_config(name) is None


def get_mcp_tools(registry: dict) -> dict:
    """Return {canonical_name: mcp_config} for tools that have MCP support.

    Verified tools (with an explicit ``MCP_CONFIG_MAP`` entry) come first.
    Every other harness in the registry gets an optimistic unverified
    default at ``~/.<tool>/mcp.json`` so ``swe mcp sync`` works between
    any two registry harnesses without per-tool code changes.
    """
    amap = alias_map(registry)
    result = {}
    for name, mcp_cfg in MCP_CONFIG_MAP.items():
        # Check if this tool is in the registry (or is an extra like claude-desktop)
        if name in registry or name in _EXTRA_ALIASES:
            result[name] = mcp_cfg
        else:
            # Check if it's an alias that resolves to something in registry
            resolved = amap.get(name)
            if resolved and resolved in MCP_CONFIG_MAP:
                result[resolved] = MCP_CONFIG_MAP[resolved]
    # Optimistic defaults: any registry harness without an explicit entry
    # is still sync-capable via the ~/.<tool>/mcp.json convention. Unsafe
    # names get no config from get_tool_config and are skipped here too.
    for name in registry:
        if name not in result:
            cfg = get_tool_config(name)
            if cfg:
                result[name] = cfg
    return result


# ── JSON helpers ──────────────────────────────────────────────────────


def _strip_jsonc(text: str) -> str:
    """Drop ``//`` and ``/* */`` comments and trailing commas.

    A ``.jsonc`` file that fails to parse reads as ``{}`` and a later
    write would emit only the synced slice, dropping every other key.
    Comments and trailing commas are the whole of the JSONC extension;
    stripping both keeps the rest of the file intact. String contents
    (``https://``, ``,}``) are preserved in both passes.
    """
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif ch == '"':
                in_str = False
            i += 1
        elif ch == '"':
            in_str = True
            out.append(ch)
            i += 1
        elif ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
        elif ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and text[i : i + 2] != "*/":
                i += 1
            i += 2
        else:
            out.append(ch)
            i += 1

    text = "".join(out)
    out = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif ch == '"':
                in_str = False
            i += 1
        elif ch == '"':
            in_str = True
            out.append(ch)
            i += 1
        elif ch == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "}]":
                i += 1  # trailing comma: drop it
            else:
                out.append(ch)
                i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def load_json(path: Path) -> dict:
    """Parse a harness MCP config file, strictly.

    These files hold far more than the MCP slice — ``~/.claude.json``
    carries the account's OAuth tokens and project state — so a file that
    fails to parse must never look like ``{}``: any caller that then saves
    would destroy everything else in it. Raises
    :class:`CorruptConfigurationError`; missing files still return ``{}``.
    Read paths (``get_tool_loader``, ``get_hub_servers``) catch it and warn;
    write paths let it propagate so a malformed file is never overwritten.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError) as exc:
        raise CorruptConfigurationError(f"Cannot read {path}: {exc}") from exc
    if path.suffix == ".jsonc":
        text = _strip_jsonc(text)
    return parse_json_object(text, path)


def save_json(path: Path, data: dict):
    """Write a harness MCP config, keeping whatever mode it already had.

    These files carry resolved API tokens. The previous tmp+rename left a
    0600 config world-readable, because the temp file was created fresh
    under the umask rather than inheriting the target's mode.
    """
    atomic_write_text(path, json.dumps(data, indent=2) + "\n", private=True)


def _skip_jsonc_ws_comments(text: str, i: int) -> int:
    """Advance past whitespace and ``//`` / ``/* */`` comments."""
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in " \t\r\n":
            i += 1
        elif ch == "/" and text[i + 1 : i + 2] == "/":
            end = text.find("\n", i)
            i = n if end == -1 else end
        elif ch == "/" and text[i + 1 : i + 2] == "*":
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
        else:
            break
    return i


def _skip_jsonc_string(text: str, i: int) -> int:
    """Advance past the string starting at ``text[i] == '"'``."""
    i += 1
    n = len(text)
    while i < n and text[i] != '"':
        i += 2 if text[i] == "\\" else 1
    return min(i + 1, n)


def _jsonc_key_span(text: str, key: str) -> tuple[int, int] | None:
    """``(start, end)`` of the value bound to top-level ``key``, or None.

    Only the value span is returned — the ``"key":`` prefix is left in
    place, so a rewrite touches nothing else in the file.
    """
    i = _skip_jsonc_ws_comments(text, 0)
    if i >= len(text) or text[i] != "{":
        return None
    i += 1
    n = len(text)
    while True:
        i = _skip_jsonc_ws_comments(text, i)
        if i >= n or text[i] == "}":
            return None
        if text[i] == ",":
            i += 1
            continue
        if text[i] != '"':
            return None
        content = text[i + 1 : _skip_jsonc_string(text, i) - 1]
        i = _skip_jsonc_ws_comments(text, _skip_jsonc_string(text, i))
        if i >= n or text[i] != ":":
            return None
        vstart = _skip_jsonc_ws_comments(text, i + 1)
        # Walk the value; it ends at the next depth-0 comma or bracket.
        depth = 0
        i = vstart
        while i < n:
            ch = text[i]
            if ch == '"':
                i = _skip_jsonc_string(text, i)
                continue
            if ch == "/" and text[i + 1 : i + 2] in ("/", "*"):
                i = _skip_jsonc_ws_comments(text, i)
                continue
            if ch in "[{":
                depth += 1
            elif ch in "]}":
                if depth == 0:
                    return (vstart, i) if content == key else None
                depth -= 1
            elif ch == "," and depth == 0:
                if content == key:
                    return (vstart, i)
                break
            i += 1
        else:
            return None


def _jsonc_last_value_end(text: str) -> int | None:
    """Offset just past the last top-level member's value, or None.

    Unlike ``text.rfind("}")``, comments and whitespace trailing the last
    member are left out of the result. Inserting ``", member"`` here keeps
    them attached to the closing brace — a plain ``rstrip`` head would
    drop the comma onto a comment line and break the file.
    """
    i = _skip_jsonc_ws_comments(text, 0)
    if i >= len(text) or text[i] != "{":
        return None
    i += 1
    n = len(text)
    last_end: int | None = None
    while True:
        i = _skip_jsonc_ws_comments(text, i)
        if i >= n or text[i] == "}":
            return last_end
        if text[i] == ",":
            i += 1
            continue
        if text[i] != '"':
            return last_end
        i = _skip_jsonc_ws_comments(text, _skip_jsonc_string(text, i))
        if i >= n or text[i] != ":":
            return last_end
        i = _skip_jsonc_ws_comments(text, i + 1)
        depth = 0
        vend: int | None = None
        while i < n:
            ch = text[i]
            if ch == '"':
                i = _skip_jsonc_string(text, i)
                vend = i
                continue
            if ch == "/" and text[i + 1 : i + 2] in ("/", "*"):
                i = _skip_jsonc_ws_comments(text, i)
                continue
            if ch in "[{":
                depth += 1
            elif ch in "]}":
                if depth == 0:
                    return vend
                depth -= 1
            elif ch == "," and depth == 0:
                break
            i += 1
            if ch not in " \t\r\n":
                vend = i
        else:
            return last_end
        last_end = vend


def _jsonc_member_indent(text: str, vstart: int) -> str:
    """Leading whitespace of the line holding the member that owns vstart."""
    line_start = text.rfind("\n", 0, vstart) + 1
    line = text[line_start:vstart]
    return line[: len(line) - len(line.lstrip())]


def splice_json_member(raw: str, key: str, value) -> str | None:
    """``raw`` with only top-level ``key``'s value replaced by ``value``.

    Every other byte of the text stays as it was; an absent member is
    appended inside the root object. Returns None when the member cannot
    be found or inserted cleanly.
    """
    rendered = json.dumps(value, indent=2, ensure_ascii=False)
    span = _jsonc_key_span(raw, key)
    if span is not None:
        vstart, vend = span
        # The span of the last member runs up to the root's closing brace;
        # keep the whitespace before that brace where it was.
        while vend > vstart and raw[vend - 1] in " \t\r\n":
            vend -= 1
        indent = _jsonc_member_indent(raw, vstart)
        return raw[:vstart] + rendered.replace("\n", "\n" + indent) + raw[vend:]
    # Member absent: append inside the root object, matching the two-
    # space indent these files use.
    close = raw.rfind("}")
    if close == -1:
        return None
    head = raw[:close].rstrip()
    member = f'"{key}": {rendered.replace(chr(10), chr(10) + "  ")}'
    if head.endswith("{") or head.endswith(","):
        return head + "\n  " + member + "\n" + raw[close:]
    # The comma must land on live JSON: when a // comment trails
    # the last member, splicing at head would park it inside the
    # comment and break the file. Insert after the value instead.
    vend = _jsonc_last_value_end(raw)
    if vend is None:
        return None
    return raw[:vend] + ",\n  " + member + "\n" + raw[vend:]


def _save_jsonc_slice(path: Path, key: str, value) -> bool:
    """Rewrite only ``key``'s value in a .jsonc file, keeping its comments.

    Returns False when the member cannot be found or inserted cleanly —
    the caller then falls back to a full reserialize (and warns).
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return False
    new_text = splice_json_member(raw, key, value)
    if new_text is None:
        return False
    atomic_write_text(path, new_text, private=True)
    return True


# ── Server helpers ────────────────────────────────────────────────────


def get_tool_loader(tool_name: str):
    """Return ``(path) -> {server_name: cfg}`` slice loader for the given tool.

    JSON tools return the slice of the file under their declared ``key``.
    codex (TOML-region) returns the slice directly because its loader
    already understands the file format and only emits MCP entries.
    Unverified tools fall back to the standard JSON loader at their
    optimistic ``~/.<tool>/mcp.json`` path.
    """
    cfg = get_tool_config(tool_name)
    if not cfg:
        return None
    if cfg.get("io_type") == "toml_region":
        def _load_toml(p):
            if not p.exists():
                return {}
            try:
                return load_codex_servers(p)
            except CorruptConfigurationError as exc:
                # Read paths degrade; writes refuse via save_codex_servers.
                print(c("yellow", f"  {exc}"), file=sys.stderr)
                return {}
        return _load_toml
    key = cfg["key"]
    def _load(p):
        if not p.exists():
            return {}
        try:
            data = load_json(p)
        except CorruptConfigurationError as exc:
            # Diagnostics go to stderr so `mcp ... --json` output stays
            # machine-readable.
            print(c("yellow", f"  {exc}"), file=sys.stderr)
            return {}
        servers = data.get(key, {})
        if not isinstance(servers, dict):
            print(
                c("yellow", f"  {p}: '{key}' is not an object — skipping {tool_name}"),
                file=sys.stderr,
            )
            return {}
        return servers
    return _load


def get_tool_saver(tool_name: str):
    """Return ``(servers, path) -> None`` writer for the given tool.

    JSON tools reconstruct the full file from existing content + the new
    slice. codex (TOML-region) only touches the contiguous ``[mcp_servers*]``
    block, preserving every other byte of ``config.toml``. Unverified
    tools use the standard JSON writer, creating the file if absent.
    """
    cfg = get_tool_config(tool_name)
    if not cfg:
        return None
    if cfg.get("io_type") == "toml_region":
        return lambda servers, p: save_codex_servers(servers, p)
    key = cfg["key"]
    def _save(servers, p):
        if p.suffix == ".jsonc" and p.exists():
            # A full reserialize would strip every comment the file
            # carries; rewrite just the managed member instead.
            if _save_jsonc_slice(p, key, servers):
                return
            try:
                raw = p.read_text(encoding="utf-8")
            except OSError:
                raw = ""
            if "//" in raw or "/*" in raw:
                print(c("yellow", f"  {p}: layout not recognised — "
                                "comments could not be preserved"),
                      file=sys.stderr)
        existing = load_json(p) if p.exists() else {}
        existing[key] = servers
        save_json(p, existing)
    return _save


def _filter_by_patterns(servers: dict, patterns: list[str]) -> tuple[dict, list[str]]:
    """Select servers whose name matches any pattern.

    Patterns are fnmatch globs, so ``dv__*`` takes a whole category and
    ``*__*`` takes every prefixed server. A bare name still works because a
    string with no wildcard only matches itself.

    Also returns the patterns that matched nothing, so a typo is reported
    rather than looking identical to an empty result.
    """
    selected: dict = {}
    unmatched: list[str] = []
    for pattern in patterns:
        hits = [k for k in servers if fnmatch.fnmatchcase(k, pattern)]
        if not hits:
            unmatched.append(pattern)
        for k in hits:
            selected[k] = servers[k]
    return selected, unmatched


# ── The hub ──────────────────────────────────────────────────────────
#
# ~/.swe/mcp.json is the middleman: `swe mcp discover --apply` pulls from
# every harness into it, `swe mcp sync swe <targets>` pushes back out. It
# is not a harness, so it has no config file of its own to read; it stores
# canonical shape directly, which is why its format is "standard".

MCP_SOURCE_KEY = "mcpServers"  # mirrors swe.mcp.discover

HUB_LABEL = "swe"
HUB_ALIASES = ("swe", "quiver", "hub", "mcp.json", ".")


def is_hub(name: str | None) -> bool:
    return bool(name) and name.lower() in HUB_ALIASES


def get_hub_servers() -> dict:
    """Canonical servers recorded in mcp.json, keyed by name."""
    try:
        data = load_json(MCP_SOURCE_FILE) or {}
    except CorruptConfigurationError as exc:
        print(c("yellow", f"  {exc}"), file=sys.stderr)
        return {}
    servers = data.get(MCP_SOURCE_KEY, {})
    return servers if isinstance(servers, dict) else {}


def save_hub_servers(servers: dict) -> None:
    data = load_json(MCP_SOURCE_FILE) or {}
    data[MCP_SOURCE_KEY] = servers
    data["updated"] = datetime.now().isoformat()
    save_json(MCP_SOURCE_FILE, data)


def _swe_state() -> dict:
    """The hub file's own ``swe`` block: state swe keeps in mcp.json."""
    try:
        data = load_json(MCP_SOURCE_FILE) or {}
    except CorruptConfigurationError:
        return {}
    state = data.get("swe")
    return state if isinstance(state, dict) else {}


def _save_swe_state(state: dict) -> None:
    data = load_json(MCP_SOURCE_FILE) or {}
    data["swe"] = state
    save_json(MCP_SOURCE_FILE, data)


def disabled_map() -> dict[str, list[str]]:
    """``{harness: [server names]}`` — the per-harness switches.

    Disabling is swe's own state, not a config flag: every harness format
    honours it the same way — the entry leaves the file and the hub keeps
    a copy to restore. ``swe mcp sync`` also honours it, so a later sync
    cannot silently turn a server back on.
    """
    disabled = _swe_state().get("disabled")
    if not isinstance(disabled, dict):
        return {}
    return {str(k): [str(n) for n in v]
            for k, v in disabled.items() if isinstance(v, list)}


def disabled_defs() -> dict[str, dict]:
    """``{harness: {name: raw entry}}`` — the copies kept at disable time.

    Enable restores a harness's own raw entry, so per-format keys the
    canonical shape drops (``startup_timeout_sec`` and friends) and
    per-harness variants survive a toggle. Credentials inside are already
    shielded to ``${NAME}`` references; the raw never reaches this file
    with a plaintext value.
    """
    defs_ = _swe_state().get("disabled_defs")
    if not isinstance(defs_, dict):
        return {}
    return {str(h): dict(n) for h, n in defs_.items() if isinstance(n, dict)}


def _mark_disabled(harness: str, name: str, raw_entry: dict | None) -> None:
    """One state write for a switch-off: the mark plus the harness's own
    (already shielded) raw copy ``swe mcp enable`` restores."""
    state = _swe_state()
    names = state.setdefault("disabled", {}).setdefault(harness, [])
    if name not in names:
        names.append(name)
    if raw_entry is not None:
        state.setdefault("disabled_defs", {}).setdefault(harness, {})[name] = raw_entry
    _save_swe_state(state)


def _mark_enabled(harness: str, name: str) -> None:
    """One state write for a switch-on: the mark and the kept raw copy go."""
    state = _swe_state()
    disabled = state.get("disabled") or {}
    names = disabled.get(harness) or []
    if name in names:
        names.remove(name)
    if not names:
        disabled.pop(harness, None)
    defs_ = state.get("disabled_defs") or {}
    harness_defs = defs_.get(harness) or {}
    harness_defs.pop(name, None)
    if not harness_defs:
        defs_.pop(harness, None)
    _save_swe_state(state)


def master_off_map() -> dict[str, list[str]]:
    """``{server: [harnesses]}`` — what a master ``swe mcp disable`` took away.

    A bare ``disable`` records the harnesses it switched off so a bare
    ``enable`` restores exactly that set: a harness turned off by hand
    (``--for``) before the master-off is not resurrected.
    """
    raw = _swe_state().get("master_off")
    if not isinstance(raw, dict):
        return {}
    return {str(k): [str(h) for h in v]
            for k, v in raw.items() if isinstance(v, list)}


def _record_master_off(name: str, harnesses: list[str]) -> None:
    """A master-off's restore set: exactly the harnesses this call switched off."""
    state = _swe_state()
    state.setdefault("master_off", {})[name] = list(harnesses)
    _save_swe_state(state)


def _drop_master_off(name: str, harnesses: list[str] | None = None) -> None:
    """Drop ``harnesses`` — the whole entry when None — from a restore set.

    Emptied lists and an emptied ``master_off`` map go with them: a stray
    ``[]`` would read as a recorded restore set of nothing.
    """
    state = _swe_state()
    master = state.get("master_off") or {}
    if name not in master:
        return
    recorded = master.get(name)
    if harnesses is None or not isinstance(recorded, list):
        master.pop(name, None)
    else:
        drop = set(harnesses)
        left = [h for h in recorded if h not in drop]
        if left:
            master[name] = left
        else:
            master.pop(name, None)
    if not master:
        state.pop("master_off", None)
    _save_swe_state(state)


def adopted_map() -> dict[str, str]:
    """``{server: harness}`` — where each hub entry was adopted from.

    Sync (and ``discover --apply``, and a ``disable`` that has to stash a
    hub copy) records the harness a pulled definition came from, so the
    provenance survives the canonicalisation the hub applies.
    """
    raw = _swe_state().get("adopted_from")
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def _record_adopted(state: dict, name: str, donor: str,
                    holders: set, mcp_tools: dict) -> None:
    """One adoption's bookkeeping inside mcp.json's ``swe`` block.

    ``adopted_from`` remembers the harness the definition came from, and a
    disabled mark for every registry harness outside ``holders`` keeps a
    later sync from pushing the server anywhere it never was — the hub
    holds it, but it stays scoped to where it already lived. The caller
    saves ``state``.
    """
    state.setdefault("adopted_from", {})[name] = donor
    disabled = state.setdefault("disabled", {})
    for tool in mcp_tools:
        if is_hub(tool) or tool in holders:
            continue
        names = disabled.setdefault(tool, [])
        if name not in names:
            names.append(name)


def _purge_server_state(name: str) -> None:
    """Drop every trace of ``name`` from mcp.json's ``swe`` block.

    ``mcp remove`` takes the hub entry away, and the disabled marks, kept
    raw copies, master-off restore set and adoption provenance all refer
    to a hub entry that no longer exists — left behind, ``mcp enable``
    could resurrect a removed server from a kept copy. Purging also means
    nothing stale steers the adoption the next sync would run should the
    server still sit in a harness file.
    """
    state = _swe_state()
    changed = False
    disabled = state.get("disabled")
    if isinstance(disabled, dict):
        for harness in [h for h, names in disabled.items()
                        if isinstance(names, list) and name in names]:
            left = [n for n in disabled[harness] if n != name]
            if left:
                disabled[harness] = left
            else:
                del disabled[harness]
            changed = True
        if not disabled:
            state.pop("disabled", None)
    defs_ = state.get("disabled_defs")
    if isinstance(defs_, dict):
        for harness in [h for h, entries in defs_.items()
                        if isinstance(entries, dict) and name in entries]:
            del defs_[harness][name]
            if not defs_[harness]:
                del defs_[harness]
            changed = True
        if not defs_:
            state.pop("disabled_defs", None)
    master = state.get("master_off")
    if isinstance(master, dict) and master.pop(name, None) is not None:
        changed = True
        if not master:
            state.pop("master_off", None)
    provenance = state.get("adopted_from")
    if isinstance(provenance, dict) and provenance.pop(name, None) is not None:
        changed = True
        if not provenance:
            state.pop("adopted_from", None)
    if changed:
        _save_swe_state(state)


def adopt_harness_servers(mcp_tools: dict, *, dry_run: bool = False,
                          post_state: dict | None = None) -> list[str]:
    """Pull harness-only servers into the hub, scoped to where they live.

    The hub is the source of truth, so a server a harness config holds but
    ``mcp.json`` does not is adopted at the end of a sync — after the
    writes, so a server the run just copied counts as held in its new
    home. The first holder's definition in sorted harness order goes in
    canonicalised and shielded like ``discover --apply`` (deterministic
    when harnesses disagree — the inventory's conflict/variants still
    reports the split), provenance lands in ``swe.adopted_from``, and the
    server is marked disabled for every registry harness not already
    holding it. Sync therefore never spreads an adopted server anywhere
    new; ``mcp enable`` is how one deliberately leaves its scope.

    ``dry_run`` writes nothing — secrets included — but still says what
    it would adopt, computed from ``post_state`` (``{tool: servers}`` the
    run plans to leave behind) so a preview agrees with the real run — a
    server the same run would prune never counts as held. Returns the
    adopted names, sorted.
    """
    found_in: dict[str, dict] = {}
    for tool in sorted(mcp_tools):
        if is_hub(tool):
            continue
        if post_state is not None and tool in post_state:
            found_in[tool] = post_state[tool]
        else:
            found_in[tool] = get_tool_servers(tool)

    try:
        data = load_json(MCP_SOURCE_FILE) or {}
    except CorruptConfigurationError as exc:
        # Never overwrite the malformed hub for housekeeping's sake.
        print(c("yellow", f"  adoption skipped: {exc}"), file=sys.stderr)
        return []
    hub = data.get(MCP_SOURCE_KEY)
    if isinstance(hub, dict):
        hub = dict(hub)
    else:
        hub = {}
        if MCP_SOURCE_KEY not in data:
            # A legacy flat hub keeps its servers at the top level. Once a
            # real ``mcpServers`` map exists every reader prefers it, so
            # writing one without folding the flat entries in would make
            # them disappear: keep them by moving them into the map.
            skip = {"swe", "updated", gateway_defaults.GATEWAY_KEY}
            for key, value in data.items():
                if key not in skip and isinstance(value, dict):
                    hub[key] = value

    donors: dict[str, str] = {}
    for tool, servers in found_in.items():
        for name in servers:
            if name not in hub and name not in donors:
                donors[name] = tool
    if not donors:
        return []

    adopted: list[str] = []
    state = data.get("swe")
    if not isinstance(state, dict):
        state = {}
        data["swe"] = state
    secrets = None if dry_run else load_secrets()
    wrote = False
    for name in sorted(donors):
        donor = donors[name]
        canonical = get_mcp_handler(donor).parse(found_in[donor][name])
        if not canonical:
            # An entry the donor's own parser cannot read stays the
            # harness's private business.
            continue
        if dry_run:
            adopted.append(name)
            continue
        try:
            shielded = _shielded_copy(redact_secrets(canonical, secrets))
        except (OSError, ValueError) as exc:
            # Housekeeping never fails the sync it rode on: a secrets
            # store that cannot take the credential just leaves this
            # server out, rather than crash after the requested writes.
            print(c("yellow", f"  {name}: could not shield its credentials "
                            f"({exc.__class__.__name__}: {exc}) — left out "
                            "of the hub"), file=sys.stderr)
            continue
        if shielded is None:
            # A credential the secrets file cannot hold stays out of the
            # versioned hub entirely rather than land in plaintext.
            continue
        hub[name] = shielded
        holders = {t for t, servers in found_in.items() if name in servers}
        _record_adopted(state, name, donor, holders, mcp_tools)
        adopted.append(name)
        wrote = True
    if wrote:
        data[MCP_SOURCE_KEY] = hub
        data["swe"] = state
        data["updated"] = datetime.now().isoformat()
        try:
            save_json(MCP_SOURCE_FILE, data)
        except OSError as exc:
            # Housekeeping never fails the sync it rode on: say so and
            # report nothing adopted, since none of it persisted.
            print(c("yellow", f"  adoption could not write "
                            f"{MCP_SOURCE_FILE}: {exc}"), file=sys.stderr)
            return []
    return adopted


def servers_for_source(name: str) -> dict:
    """Servers to sync FROM, in that source's own on-disk shape.

    Harnesses return raw config, not canonical: callers convert with
    ``convert_server_for_target``, and ``--strict`` relies on seeing the
    unparsed original to detect a lossy conversion. Parsing here would
    normalise the very thing strict mode is meant to catch.

    The hub is already canonical, and its format is declared "standard", so
    the same conversion path handles it without a special case.
    """
    if not is_hub(name):
        return get_tool_servers(name)
    # The hub stores ${NAME} references; a harness needs the real value to
    # start the server, and most never expand variables themselves. A name
    # the store cannot satisfy is refused by sync before this is reached.
    return resolve_secrets(get_hub_servers())


def get_tool_servers(tool_name: str) -> dict:
    cfg = get_tool_config(tool_name)
    if not cfg:
        return {}
    loader = get_tool_loader(tool_name)
    if loader is None:
        return {}
    return loader(cfg["path"])


def get_tool_format(tool_name: str) -> str:
    if is_hub(tool_name):
        return "standard"
    return get_tool_config(tool_name).get("format", "standard")


def get_mcp_handler(tool_name: str) -> McpFormatHandler:
    return get_format_handler(get_tool_format(tool_name))


def get_tool_servers_canonical(tool_name: str) -> dict:
    """Return {server_name: canonical_server_dict} for a tool."""
    handler = get_mcp_handler(tool_name)
    out = {}
    for name, raw_cfg in get_tool_servers(tool_name).items():
        if isinstance(raw_cfg, dict):
            out[name] = handler.parse(raw_cfg)
    return out


def normalize_server(cfg: dict) -> dict:
    """Normalize mixed tool formats to canonical standard MCP shape."""
    return normalize_server_any(cfg)


def convert_server_for_target(cfg: dict, source_tool: str, target_tool: str) -> dict:
    """Convert server config between tool-specific MCP formats via handlers."""
    return convert_server_between_formats(
        cfg,
        source_format=get_tool_format(source_tool),
        target_format=get_tool_format(target_tool),
    )


# Canonical data fields — everything else in a target entry is
# format-specific metadata (opencode's ``enabled``, droid's ``type``,
# claude's ``disabled``/``autoApprove``, timeouts) that must survive an
# overwrite, because emit stamps its own defaults for some of them.
_CANONICAL_SERVER_KEYS = frozenset(
    {"command", "args", "env", "environment", "url", "headers"}
)


def _merge_format_keys(existing: dict, converted: dict) -> dict:
    """Overwrite the data fields but keep the target's own extension keys.

    Without this, syncing over an opencode server marked ``enabled: false``
    silently re-enables it, a droid ``type: sse`` server becomes ``http``,
    and claude's ``autoApprove`` list vanishes.
    """
    if not isinstance(existing, dict):
        return converted
    out = dict(converted)
    for k, v in existing.items():
        if k in _CANONICAL_SERVER_KEYS:
            continue
        if k == "type" and ("url" in converted
                            or ("url" in existing) != ("url" in converted)):
            # "type" describes the transport (droid's sse/http, opencode's
            # local/remote). The converted definition's transport is the
            # source of truth: keeping a stale "sse" beside http — or a
            # stale "http" beside sse — would lie about the connection.
            continue
        out[k] = v
    return out


LOCAL_ONLY_NOTE = ("takes local servers only in its config file; add a remote one "
                   "in the app itself (Settings > Connectors)")


def takes_remote(tool: str) -> bool:
    """Whether ``tool``'s config can hold a server reached at a URL."""
    return not get_tool_config(tool).get("local_only")


def server_type(cfg: dict) -> str:
    cfg = normalize_server(cfg)
    if cfg.get("url"):
        return "http"
    if cfg.get("command"):
        if "mcp-remote" in cfg.get("args", []):
            return "wrapped"
        return "stdio"
    return "unknown"


def server_summary(cfg: dict) -> str:
    cfg = normalize_server(cfg)
    st = server_type(cfg)
    if st == "http":
        # Harness configs hold resolved values — a `?key=TOKEN` URL would
        # otherwise print its credential to the terminal. scrub_url hides
        # every credential shape, not only values already in the store.
        url = cfg.get("url", "?")
        url = scrub_url(url) if isinstance(url, str) else url
        return f"http → {sanitize(url)}"
    if st == "stdio":
        cmd = cfg.get("command", "?")
        args = scrub_args(cfg.get("args", []))
        parts = [cmd]
        for a in args:
            if not a.startswith("-"):
                parts.append(a)
                break
        return " ".join(sanitize(str(p)) for p in parts)
    if st == "wrapped":
        # Hand-edited args can hold numbers (`"--port", 3334`): skip them
        # rather than crash `swe mcp doctor` and discover on one entry.
        url = next((a for a in cfg.get("args", [])
                    if isinstance(a, str) and a.startswith("http")), "?")
        return f"wrapped → {sanitize(scrub_url(url))}"
    return "unknown"


def _head_ok(url: str) -> bool:
    """True when a HEAD request to ``url`` is answered within five seconds."""
    import urllib.request

    try:
        urllib.request.urlopen(
            urllib.request.Request(url, method="HEAD"), timeout=5)
    except Exception:
        return False
    return True


def probe_server_health(name: str, cfg: dict) -> tuple[str, str | None]:
    """Run the health check for one server: ``(state, detail)``.

    ``state`` is ``ok``, ``error`` or ``unknown``. ``detail`` says what is
    wrong, and is None when nothing is. Details name env vars, never their
    values. Both ``swe mcp status`` renderings come from here, so the table
    and ``--json`` cannot disagree.
    """
    cfg = normalize_server(cfg)
    st = server_type(cfg)

    if st == "http":
        url = cfg.get("url", "")
        if not (isinstance(url, str)
                and url.lower().startswith(("http://", "https://"))):
            return "error", "invalid URL scheme"
        if _head_ok(url):
            return "ok", None
        return "error", "url unreachable"

    if st == "stdio":
        cmd = cfg.get("command", "")
        if cmd.startswith("/") or cmd.startswith("~"):
            resolved = os.path.expanduser(cmd)
            if not (os.path.isfile(resolved) and os.access(resolved, os.X_OK)):
                return "error", "binary not found"
        else:
            if not shutil.which(cmd):
                # ``cmd`` is config text: a name carrying escapes would
                # otherwise print them straight to the terminal.
                return "error", f"'{sanitize(cmd)}' not in PATH"
        env = cfg.get("env", {})
        missing = [k for k, v in env.items()
                   if not v or (isinstance(v, str) and v.startswith("YOUR_"))]
        if missing:
            shown = ", ".join(sanitize(k) for k in missing)
            return "error", f"missing env: {shown}"
        return "ok", None

    if st == "wrapped":
        args = cfg.get("args", [])
        url = next((a for a in args if isinstance(a, str)
                    and a.lower().startswith(("http://", "https://"))), None)
        if url:
            if _head_ok(url):
                return "ok", None
            return "error", "url unreachable"
        return "error", "no url found"

    return "unknown", "no command or url"


# Health checks run side by side: an HTTP server's HEAD probe can take its
# whole five-second timeout, and `swe mcp status` waited on each in turn.
_HEALTH_PROBE_WORKERS = 8


def _probe_all(probe, servers: list[tuple[str, dict]]) -> list:
    """``probe(name, cfg)`` for each server, on a small thread pool, the
    results in the order given (#1537)."""
    if len(servers) < 2:
        return [probe(name, cfg) for name, cfg in servers]
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=min(_HEALTH_PROBE_WORKERS,
                                            len(servers))) as pool:
        return list(pool.map(lambda pair: probe(*pair), servers))


def check_server_health(name: str, cfg: dict) -> str:
    state, detail = probe_server_health(name, cfg)
    if state == "ok":
        return c("green", "✓")
    if state == "error":
        return c("red", f"✗ {detail}")
    return c("yellow", "? unknown")


def resolve_tool_arg(registry: dict, arg: str) -> str | None:
    """Resolve a CLI arg to a canonical tool name that has MCP support.

    Any harness in the registry is accepted — verified tools use their
    explicit ``MCP_CONFIG_MAP`` entry, unverified tools get the
    optimistic ``~/.<tool>/mcp.json`` default. Extras like
    ``claude-desktop`` (not in the registry) are accepted directly.
    """
    if is_hub(arg):
        return HUB_LABEL
    resolved = resolve(registry, arg)
    if resolved:
        return resolved
    # Extras not in the registry (e.g. claude-desktop, codex via cx-toml).
    if arg in MCP_CONFIG_MAP:
        return arg
    return None


# ── Subcommands ──────────────────────────────────────────────────────


def _display_tools(mcp_tools: dict) -> dict:
    """Filter ``get_mcp_tools`` output for matrix-style display.

    Unverified harnesses whose optimistic config path does not exist are
    hidden from `swe mcp list` / `status` / `validate` to keep the matrix
    readable. They remain fully sync-capable — name one explicitly to
    inspect it, or let `swe mcp sync` create the file.
    """
    return {
        name: cfg for name, cfg in mcp_tools.items()
        if not cfg.get("unverified") or cfg["path"].exists()
    }


def _tool_header(name: str, width: int) -> str:
    """A tool name centred in its column, trimmed to fit.

    An ellipsis costs three of the few characters a narrow column has, so
    below eight it is dropped: "claud" identifies the harness where
    "c..." does not.
    """
    label = name if len(name) <= width else (
        name[:width] if width < 8 else truncate(name, width))
    return label.center(width)


def cmd_list(args):
    args, json_view = take_json_flag(list(args))
    if args and args[0] in ("-h", "--help", "help"):
        print(MCP_HELP["list"])
        return 0
    registry = load_registry()
    mcp_tools = get_mcp_tools(registry)
    # Under --json stdout is one document, so refusals go to stderr.
    refusal = error_stream(json_view)

    target = None
    if args:
        target = resolve_tool_arg(registry, args[0])
        if _unsafe_registry_name(target):
            print(c("red", f"Unsafe harness name in registry: {args[0]}"),
                  file=refusal)
            return 1
        if not target:
            print(f"Unknown tool: {args[0]}", file=refusal)
            print(f"Available: {', '.join(mcp_tools.keys())}", file=refusal)
            return 1

    # The value is never read — only the name loops into get_tool_servers,
    # which resolves the hub and unverified names on its own.
    tools = {target: mcp_tools.get(target, {})} if target else _display_tools(mcp_tools)

    all_servers = set()
    tool_data = {}
    for name in tools:
        servers = get_tool_servers(name)
        tool_data[name] = servers
        all_servers.update(servers.keys())

    if json_view:
        # Every harness that was read, server or not, so a caller can draw
        # the whole matrix and see which harnesses lack a server.
        return emit({"harnesses": list(tool_data),
                     "servers": [row for row, _ in server_inventory(tool_data, include_hub=target is None)]})

    if not all_servers:
        print("No MCP servers found.")
        return 0

    sorted_servers = sorted(all_servers)
    tool_names = list(tools.keys())
    # Pre-measure widths. SERVER column = max(header_label, longest
    # server name); tool column width = max(header_label, longest
    # tool name). The same value flows to both Table.add_column and
    # cpad so column and cpad agree by construction.
    server_width = max(
        len("SERVER"),
        max((len(s) for s in sorted_servers), default=0),
    )
    # One column per tool, so the grid grows with the harness count rather
    # than with any text in it. A tick needs one character; the width only
    # exists to fit the header, so shorten headers before the row wraps.
    col_width = max(
        len("SERVER"),
        max((len(t) for t in tool_names), default=0),
    )
    if tool_names:
        # The gap between columns is part of the budget, and SERVER can give
        # ground too: it is a name, and elide keeps both ends readable.
        gap = 2
        server_width = min(server_width, max(8, terminal_width() // 4))
        room = terminal_width() - server_width - gap * len(tool_names) - 2
        col_width = max(3, min(col_width, room // len(tool_names)))

    table = Table()
    # SERVER column: kind="text" left-aligns + pads plain server
    # names to server_width.
    table.add_column(
        "server", "SERVER", width=server_width,
        kind="text",
    )
    # Tool columns: kind="preformatted" + trust_cell_width=True so
    # the cpad-padded ✓/— cells arrive at exactly col_width visible
    # chars. Header labels are pre-centered (``name.center(col_w)``)
    # so the rendered line matches the original f-string ``^{col_w}``
    # alignment without needing a custom kind.
    for t in tool_names:
        table.add_column(
            f"tool_{t}", _tool_header(sanitize(t), col_width),
            width=col_width,
            kind="preformatted", trust_cell_width=True,
        )

    for server in sorted_servers:
        row = {"server": server}
        for t in tool_names:
            if server in tool_data[t]:
                row[f"tool_{t}"] = cpad("green", "✓", col_width)
            else:
                row[f"tool_{t}"] = cpad("dim", "—", col_width)
        table.add_row(row)

    for line in table.render():
        print(line)
    print(f"\n{len(sorted_servers)} servers across {len(tool_names)} tools")
    return 0


def cmd_status(args):
    args, json_view = take_json_flag(list(args))
    if args and args[0] in ("-h", "--help", "help"):
        print(MCP_HELP["status"])
        return 0
    registry = load_registry()
    mcp_tools = get_mcp_tools(registry)
    # Under --json stdout is one document, so refusals go to stderr.
    refusal = error_stream(json_view)

    target = None
    if args:
        target = resolve_tool_arg(registry, args[0])
        if _unsafe_registry_name(target):
            print(c("red", f"Unsafe harness name in registry: {args[0]}"),
                  file=refusal)
            return 1
        if not target:
            print(f"Unknown tool: {args[0]}", file=refusal)
            return 1

    tools = {target: mcp_tools.get(target, {})} if target else _display_tools(mcp_tools)

    all_servers = set()
    tool_data = {}
    for name in tools:
        servers = get_tool_servers(name)
        tool_data[name] = servers
        all_servers.update(servers.keys())

    if json_view:
        inventory = list(server_inventory(tool_data, include_hub=target is None))
        probes = _probe_all(probe_server_health,
                            [(row["name"], cfg) for row, cfg in inventory])
        rows = []
        for (row, _cfg), (state, detail) in zip(inventory, probes):
            row["health"], row["detail"] = state, detail
            rows.append(row)
        return emit({"harnesses": list(tool_data), "servers": rows})

    if not all_servers:
        print("No MCP servers found.")
        return 0

    sorted_servers = sorted(all_servers)
    tool_names = list(tools.keys())
    # Pre-measure widths. SERVER column + tool columns are the same
    # pre-measure pattern as cmd_list. HEALTH column is the longest
    # health-string visible_len across the row set.
    server_width = max(
        len("SERVER"),
        max((len(s) for s in sorted_servers), default=0),
    )
    # One column per tool, so the grid grows with the harness count rather
    # than with any text in it. A tick needs one character; the width only
    # exists to fit the header, so shorten headers before the row wraps.
    col_width = max(
        len("SERVER"),
        max((len(t) for t in tool_names), default=0),
    )
    if tool_names:
        # The gap between columns is part of the budget, and SERVER can give
        # ground too: it is a name, and elide keeps both ends readable.
        gap = 2
        server_width = min(server_width, max(8, terminal_width() // 4))
        room = terminal_width() - server_width - gap * len(tool_names) - 2
        col_width = max(3, min(col_width, room // len(tool_names)))

    # Pre-compute health strings first (they drive HEALTH width) so
    # we can include HEALTH in column-widths math. Since the union
    # ``all_servers`` guarantees every server exists in at least one
    # tool's servers dict, ``first_cfg`` is always present.
    health_strings = _probe_all(check_server_health, [
        (server, next(tool_data[t][server] for t in tool_names
                      if server in tool_data[t]))
        for server in sorted_servers
    ])
    health_width = max(
        len("HEALTH"),
        max((visible_len(h) for h in health_strings), default=0),
    )

    table = Table()
    table.add_column(
        "server", "SERVER", width=server_width,
        kind="text",
    )
    for t in tool_names:
        table.add_column(
            f"tool_{t}", _tool_header(sanitize(t), col_width),
            width=col_width,
            kind="preformatted", trust_cell_width=True,
        )
    # HEALTH column. Header label is left-aligned (preserves the
    # original f-string ``<{health_w}`` alignment). The column uses
    # ``kind="preformatted"`` + ``trust_cell_width=True`` + ``fit="content"``
    # so the rightmost column grows to fit the longest health string
    # (which may exceed 20 chars for ``✗ missing env: FOO,BAR`` etc.).
    table.add_column(
        "health", "HEALTH", width=health_width,
        kind="preformatted", trust_cell_width=True, fit="content",
    )

    for server, health in zip(sorted_servers, health_strings):
        row = {"server": server, "health": health}
        for t in tool_names:
            if server in tool_data[t]:
                row[f"tool_{t}"] = cpad("green", "✓", col_width)
            else:
                row[f"tool_{t}"] = cpad("dim", "—", col_width)
        # HEALTH cells already carry ANSI from check_server_health();
        # ``cpad`` would re-wrap their colour escapes in a generic
        # colour, so we manually pad to ``health_width`` (mirroring
        # the cmd_session alignment fix). Trailing spaces come AFTER
        # the ANSI reset so they're plain — visible_len matches the
        # column width and the row aligns with the dim HEALTH header.
        row["health"] = health + " " * (
            health_width - visible_len(health)
        )
        table.add_row(row)

    for line in table.render():
        print(line)
    print(f"\n{len(sorted_servers)} servers across {len(tool_names)} tools")
    return 0


def cmd_sync(args):
    if args and args[0] in ("-h", "--help", "help"):
        print(MCP_HELP["sync"])
        return 0
    args, json_view = take_json_flag(list(args))
    if not json_view:
        return _sync(args, None)
    # --json: stdout holds one document, so everything the sync says on the
    # way goes to stderr, and nobody is at a terminal to pick or confirm.
    report = {"source": None, "dry_run": "--dry-run" in args,
              "selected": [], "targets": []}
    with redirect_stdout(sys.stderr):
        code = _sync([*args, "--no-interactive"], report)
    if code:
        return code
    return emit(report)


def _sync(args, report: dict | None, skip_missing: bool = False):
    """The body of ``swe mcp sync``; fills ``report`` when one is given.

    ``report`` takes names, never configs: the server definitions carry
    resolved secrets.

    A hub server whose ``${NAME}`` has no value stops the whole run, so
    nothing is half synced behind the user's back. ``skip_missing`` (what
    ``swe setup --hub`` runs, never a flag) leaves just those servers out
    instead and names them in ``report["missing"]`` as ``{server:
    [NAME, ...]}``.

    A successful run ends by adopting harness-only servers into the hub
    (see ``adopt_harness_servers``); ``report["adopted"]`` lists them and
    ``--no-adopt`` skips the pass.
    """
    allowed_flags = {"--force", "--skip-conflicts", "--all", "--no-interactive", "--dry-run", "--strict", "--prune", "--no-adopt"}
    unknown_flags = [
        a for a in args
        if a.startswith("--")
        and not a.startswith("--only=")
        and not a.startswith("--except=")
        and a not in allowed_flags
    ]
    if unknown_flags:
        print(f"Unknown flag(s): {', '.join(unknown_flags)}")
        return 1

    registry = load_registry()
    mcp_tools = get_mcp_tools(registry)

    force = "--force" in args
    skip_conflicts = "--skip-conflicts" in args
    all_targets = "--all" in args
    no_interactive = "--no-interactive" in args
    dry_run = "--dry-run" in args
    strict = "--strict" in args
    prune = "--prune" in args
    adopt = "--no-adopt" not in args
    # A dry run's planned post-sync contents per target, so adoption's
    # preview counts holders the way the real run's rescan would — a
    # server the same pass would prune never counts as held.
    planned_servers: dict[str, dict] = {}

    def _finish(code: int) -> int:
        """Adoption is a sync's last act; every successful exit funnels here."""
        adopted: list[str] = []
        if code == 0 and adopt:
            adopted = adopt_harness_servers(
                mcp_tools, dry_run=dry_run,
                post_state=planned_servers if dry_run else None)
            if adopted:
                verb = "Would adopt" if dry_run else "Adopted"
                print(f"\n  {verb} {len(adopted)} server(s) into the hub: "
                      f"{', '.join(adopted)}")
                print(f"    {c('dim', 'marked off for every harness not already holding them')}")
        if report is not None:
            report["adopted"] = adopted
        return code

    only_flag = None
    except_flag = None
    for a in args:
        if a.startswith("--only="):
            only_flag = [s for s in a.split("=", 1)[1].split(",") if s]
        elif a.startswith("--except="):
            except_flag = [s for s in a.split("=", 1)[1].split(",") if s]

    positional = [a for a in args if not a.startswith("--")]

    if not positional or (len(positional) < 2 and not all_targets):
        print(f"Usage: {c('cyan', 'swe mcp sync <source> <target...> [--only=a,b] [--force|--skip-conflicts] [--dry-run] [--strict]')}")
        print(f"       {c('cyan', 'swe mcp sync <source> --all [--only=a,b] [--force|--skip-conflicts] [--dry-run] [--strict]')}")
        print(f"\nRun {c('cyan', 'swe mcp sync help')} for details.")
        return 1

    source = resolve_tool_arg(registry, positional[0])
    if _unsafe_registry_name(source):
        print(c("red", f"Unsafe harness name in registry: {positional[0]}"))
        return 1
    if not source:
        print(f"Unknown source tool: {positional[0]}")
        return 1
    if report is not None:
        # Set before any early return, so a no-op run still names its source
        # and the adopted list reads as empty rather than missing.
        report["source"] = source
        report["adopted"] = []

    targets: list[str] = []
    seen: set[str] = set()
    seen.add(source)

    if all_targets:
        # Broadcast only to verified peers + unverified tools that already
        # have a config file. Naming an unverified target explicitly still
        # works (and creates the file); --all avoids surprising users with
        # 19 new ~/.<tool>/mcp.json files in one shot.
        candidates = [
            t for t in _display_tools(mcp_tools) if t != source and not is_hub(t)
        ]
    else:
        candidates = []
        for a in positional[1:]:
            resolved = resolve_tool_arg(registry, a)
            if _unsafe_registry_name(resolved):
                # Resolves, but its name cannot form a safe ~/.<name>/
                # config path — a malformed registry entry, not a tool.
                print(c("red", f"Unsafe harness name in registry: {a}"))
                return 1
            if not resolved:
                print(f"Unknown target tool: {a}")
                return 1
            if is_hub(resolved):
                # Data flows harness -> hub by sync's adoption pass (or
                # `discover --apply` for a scanned config), hub -> harness
                # via sync. Writing tool-shaped configs into the canonical
                # file would corrupt the one thing everything else reads.
                print(
                    f"{HUB_LABEL} is the hub, not a sync target — a sync "
                    "adopts harness-only servers into it on its own."
                )
                return 1
            candidates.append(resolved)

    for t in candidates:
        if t not in seen:
            seen.add(t)
            targets.append(t)

    if not targets:
        print("No valid target tools provided.")
        return 1

    source_servers = servers_for_source(source)
    if not source_servers:
        if is_hub(source):
            print(f"{MCP_SOURCE_FILE} has no servers. Run `swe mcp discover --apply` first.")
        else:
            print(f"{source} has no MCP servers.")
        return _finish(0)

    disabled = disabled_map()

    # Every name the source could have offered, before --only/--except
    # narrow it: --prune may only delete inside this pool, so a filtered
    # run never removes servers it was never asked to look at.
    pool_names = set(source_servers)

    if only_flag:
        source_servers, unmatched = _filter_by_patterns(source_servers, only_flag)
        if unmatched:
            print(c("yellow", f"  --only matched nothing: {', '.join(unmatched)}"))
        if not source_servers:
            print(f"No matching servers found in {source}.")
            return _finish(0)

    if except_flag:
        excluded, unmatched = _filter_by_patterns(source_servers, except_flag)
        if unmatched:
            print(c("yellow", f"  --except matched nothing: {', '.join(unmatched)}"))
        source_servers = {k: v for k, v in source_servers.items() if k not in excluded}
        if not source_servers:
            print(f"--except removed every server from {source}.")
            return 1

    server_names = sorted(source_servers.keys())
    target_server_names = set()
    for target in targets:
        target_server_names.update(get_tool_servers(target).keys())

    interactive = (not no_interactive) and sys.stdin.isatty() and sys.stdout.isatty()

    if interactive:
        picked = multiselect(
            [Choice(key=n, label=n) for n in server_names],
            selected=target_server_names,
            title=f"Copy from {source} → {', '.join(targets)}",
        )
        selected = picked or []
    else:
        selected = server_names
        print(f"{c('dim', 'Non-interactive mode: selecting all eligible servers.')}")

    if not selected:
        print("Nothing selected.")
        return _finish(0)
    if report is not None:
        report["selected"] = list(selected)

    if is_hub(source):
        # A ${NAME} the store can't satisfy would reach the server as a
        # literal "${NAME}" string — refuse rather than write it. Works on
        # the unresolved hub configs, not the already-resolved copies above.
        secrets = load_secrets()
        hub_servers = get_hub_servers()
        missing: dict[str, list[str]] = {}
        any_remote_ok = any(takes_remote(t) for t in targets)
        # A server every target holds off is never written, so its missing
        # secrets and lossy conversions cannot block the sync either.
        writeable = [
            n for n in selected
            if any(n not in set(disabled.get(t, [])) for t in targets)
        ]
        for server_name in writeable:
            if not any_remote_ok and server_type(source_servers[server_name]) == "http":
                # No target can take this one: it is left out below, so
                # its secrets don't matter to this run.
                continue
            for var in unresolved_names(hub_servers.get(server_name, {}), secrets):
                missing.setdefault(var, []).append(server_name)
        if missing and skip_missing:
            left_out: dict[str, list[str]] = {}
            for var in sorted(missing):
                for server_name in missing[var]:
                    left_out.setdefault(server_name, []).append(var)
            if report is not None:
                report["missing"] = left_out
            selected = [name for name in selected if name not in left_out]
            if report is not None:
                report["selected"] = list(selected)
            if not selected:
                return _finish(0)
        elif missing:
            for var in sorted(missing):
                used_by = ", ".join(missing[var])
                print(f"Not syncing: no value for {var} (used by {used_by}). "
                      f"Set it with: swe mcp secret set {var}")
            return 1

    # Shared hub servers go out as the gateway's URL while it is on.
    gateway = (gateway_defaults.settings_of(load_json(MCP_SOURCE_FILE))
               if is_hub(source) else {"enabled": False, "port": 0})

    print(f"\n{c('green', f'Selected {len(selected)} server(s)')} from {c('cyan', source)}")
    if dry_run:
        print(f"{c('yellow', 'Dry-run mode: no files will be modified.')}\n")

    if strict:
        source_format = get_tool_format(source)
        strict_errors = []
        for target in targets:
            target_format = get_tool_format(target)
            for name in selected:
                if name in disabled.get(target, []):
                    continue  # this pair is held off — nothing is written
                issues = get_conversion_issues(source_servers[name], source_format, target_format)
                if issues:
                    strict_errors.append((target, name, issues))

        if strict_errors:
            print(c("red", "Strict mode blocked sync due to lossy conversion:"))
            for target, name, issues in strict_errors[:20]:
                print(f"  {c('cyan', sanitize(target))}:{sanitize(name)} -> "
                      + "; ".join(issues))
            if len(strict_errors) > 20:
                print(f"  ... and {len(strict_errors) - 20} more")
            print(c("dim", "Tip: rerun without --strict to proceed."))
            return 1

    for target in targets:
        target_cfg = get_tool_config(target)
        target_servers = get_tool_servers(target)
        held_off = sorted(set(selected) & set(disabled.get(target, [])))
        target_selected = [n for n in selected if n not in set(held_off)]
        if target_cfg.get("unverified") and not target_cfg["path"].exists():
            print(
                f"\n  {c('yellow', 'Note')}: {c('cyan', target)} has no verified MCP "
                f"config path; writing to the optimistic default "
                f"{c('dim', str(target_cfg['path']))}."
            )

        conflicts = [s for s in target_selected if s in target_servers]
        overwrite = set()

        if conflicts:
            if force:
                overwrite = set(conflicts)
            elif skip_conflicts:
                overwrite = set()
            elif interactive:
                print(f"\n  {c('yellow', f'{len(conflicts)} conflict(s)')} with {c('cyan', target)}:\n")
                for name in conflicts:
                    src_summary = server_summary(source_servers[name])
                    tgt_summary = server_summary(target_servers[name])
                    print(f"    {c('bold', sanitize(name))}")
                    print(f"      {source}: {c('dim', src_summary)}")
                    print(f"      {target}: {c('dim', tgt_summary)}")

                picked = multiselect(
                    [Choice(key=n, label=n) for n in conflicts],
                    selected=conflicts,
                    title=f"Overwrite conflicting servers in {target}? ({source} → {target})",
                )
                overwrite = set(picked or [])
            else:
                print(f"{c('yellow', f'{target}: {len(conflicts)} conflict(s) skipped in non-interactive mode (use --force to overwrite).')}")

        added: list[str] = []
        updated: list[str] = []
        skipped: list[str] = []
        # Remote servers a local-only config can't hold: left out, said so.
        unsupported: list[str] = []
        remote_ok = takes_remote(target)

        via_gateway: list[str] = []

        for name in target_selected:
            server = source_servers[name]
            on_gateway = remote_ok and gateway_defaults.runs_on_gateway(server, gateway)
            if on_gateway:
                server = gateway_defaults.entry_for(name, gateway["port"])
            if not remote_ok and server_type(server) == "http":
                unsupported.append(name)
                continue
            converted = convert_server_for_target(server, source, target)
            if name in target_servers:
                if force or name in overwrite:
                    target_servers[name] = _merge_format_keys(
                        target_servers[name], converted
                    )
                    updated.append(name)
                else:
                    skipped.append(name)
                    continue
            else:
                target_servers[name] = converted
                added.append(name)
            if on_gateway:
                # Only what this run wrote: a skipped entry keeps whatever
                # the target already had.
                via_gateway.append(name)

        # --only/--except or a picker deselection narrowed the run.
        narrowed = (
            bool(only_flag or except_flag)
            or set(selected) != set(server_names)
        )
        pruned: list[str] = []
        if prune:
            # Narrowed runs prune only inside the source's pool — names it
            # offered but this selection dropped — never entries the source
            # doesn't know, which are the user's own. Unfiltered runs keep
            # the mirror semantics: anything off the selection goes.
            if narrowed:
                target_only = sorted(
                    set(target_servers) & (pool_names - set(selected))
                )
            else:
                target_only = sorted(set(target_servers.keys()) - set(selected))
            for name in target_only:
                del target_servers[name]
                pruned.append(name)

        if report is not None:
            report["targets"].append({"name": target, "added": added,
                                      "updated": updated, "skipped": skipped,
                                      "pruned": pruned, "unsupported": unsupported,
                                      "via_gateway": via_gateway,
                                      "disabled": held_off})

        if dry_run:
            # The in-memory map already holds adds, skips and prunes —
            # adoption's preview reads it instead of the untouched file.
            planned_servers[target] = dict(target_servers)
        elif added or updated or pruned:
            saver = get_tool_saver(target)
            if saver is not None:
                saver(target_servers, target_cfg["path"])

        parts = []
        if added:
            parts.append(f"{len(added)} {'would add' if dry_run else 'added'}")
        if updated:
            parts.append(f"{len(updated)} {'would update' if dry_run else 'updated'}")
        if skipped:
            parts.append(f"{len(skipped)} {'would skip' if dry_run else 'skipped'}")
        if pruned:
            # A narrowed run names what it removed — a delete that can no
            # longer be inferred from the selection must not be silent.
            names = f" ({', '.join(pruned)})" if narrowed else ""
            parts.append(
                f"{len(pruned)} {'would prune' if dry_run else 'pruned'}{names}"
            )
        if via_gateway:
            parts.append(f"{len(via_gateway)} through the gateway")
        if held_off:
            parts.append(f"{len(held_off)} left off (disabled: {', '.join(held_off)})")
        if unsupported:
            parts.append(f"{len(unsupported)} remote left out ({', '.join(unsupported)}): "
                         f"{get_tool_config(target).get('label', target)} {LOCAL_ONLY_NOTE}")

        if parts:
            status = c('cyan', '•') if dry_run else c('green', '✓')
            print(f"\n  {status} {target}: {', '.join(parts)}")
        else:
            print(f"\n  {target}: nothing changed")

    return _finish(0)


def sync_hub_to(target: str) -> dict:
    """``swe mcp sync swe <target> --no-interactive --skip-conflicts`` in
    process, for ``swe setup --hub``: says nothing, and a server whose
    ``${NAME}`` has no value is left out (``missing``) rather than
    stopping the rest.

    Returns ``swe mcp sync --json``'s report for that one target plus
    ``missing`` (``{server: [NAME]}``), ``code`` (the exit status) and
    ``said`` (what the sync would have printed, for a failure's reason).
    A server already in the target is left as it is (``skipped``).
    Raises what the sync raises, a corrupt target config among them.
    """
    report = {"source": None, "dry_run": False, "selected": [], "targets": [],
              "missing": {}}
    said = io.StringIO()
    with redirect_stdout(said):
        code = _sync([HUB_LABEL, target, "--no-interactive", "--skip-conflicts"],
                     report, skip_missing=True)
    report["code"] = code
    report["said"] = strip_ansi(said.getvalue()).strip()
    return report


_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]*$")


def cmd_add(args):
    # Split at the first `--` before any flag handling, so a command arg
    # like `--json` or `--to=y` stays part of the command, verbatim.
    command_tail = None
    if "--" in args:
        cut = args.index("--")
        command_tail, args = args[cut + 1:], args[:cut]
    args, json_view = take_json_flag(list(args))
    if args and args[0] in ("-h", "--help", "help"):
        print(MCP_HELP["add"])
        return 0
    refusal = error_stream(json_view)

    try:
        args = expand_value_flags(
            args, {"--to", "--env", "--header", "--url", "--transport"})
    except ValueError as exc:
        print(c("red", str(exc)), file=refusal)
        return 1

    positional: list[str] = []
    to_names: list[str] = []
    env_pairs: list[str] = []
    header_pairs: list[str] = []
    url = transport = None
    force = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("--to", "--env", "--header", "--url", "--transport"):
            value = args[i + 1]
            i += 2
            if arg == "--to":
                to_names += [s for s in value.split(",") if s]
            elif arg == "--env":
                env_pairs.append(value)
            elif arg == "--header":
                header_pairs.append(value)
            elif arg == "--url":
                url = value
            else:
                transport = value
        elif arg == "--force":
            force = True
            i += 1
        elif arg.startswith("--"):
            print(f"Unknown flag: {arg}", file=refusal)
            return 1
        else:
            positional.append(arg)
            i += 1

    if len(positional) != 1 or not _SERVER_NAME_RE.match(positional[0]):
        print(f"Usage: {c('cyan', 'swe mcp add <name> (--url=<url> | -- <command> [args…])')}",
              file=refusal)
        print("A name is letters, digits, . _ @ - and can't start with _ . @ -.",
              file=refusal)
        return 1
    name = positional[0]

    try:
        env = _add_pairs(env_pairs, "--env")
        headers = _add_pairs(header_pairs, "--header")
        canonical, targets = add_plan(load_registry(), url=url, transport=transport,
                                      command=command_tail, env=env, headers=headers,
                                      to_names=to_names)
    except AddRefused as exc:
        print(c("red", str(exc)) if exc.alarming else str(exc), file=refusal)
        return 1

    # A literal credential in mcp.json is versioned; nudge toward ${NAME}.
    for key, value in {**env, **headers}.items():
        if "${" not in value and _SECRET_FLAG_RE.search(key):
            print(c("yellow",
                    f"  {key} looks like a credential and goes into mcp.json, "
                    f"which is versioned. Use ${{{key}}} and swe mcp secret set {key}."),
                  file=sys.stderr)

    try:
        report = add_server(name, canonical, targets, force=force)
    except AddRefused as exc:
        print(c("red", str(exc)) if exc.alarming else str(exc), file=refusal)
        return 1

    if json_view:
        return emit(report)
    print(f"{report['hub'].capitalize()} {name} in the SWE hub (~/.swe/mcp.json)")
    for r in report["targets"]:
        print(f"  {r['status']} in {r['name']}")
    return 0


class AddRefused(ValueError):
    """An ``swe mcp add`` swe won't do; ``alarming`` prints in red."""

    def __init__(self, message: str, alarming: bool = False):
        super().__init__(message)
        self.alarming = alarming


def _add_pairs(raw: list[str], flag: str) -> dict[str, str]:
    out = {}
    for pair in raw:
        key, sep, value = pair.partition("=")
        if not sep:
            raise AddRefused(f"{flag} takes KEY=VALUE pairs: {pair!r} has no =")
        out[key] = value
    return out


def add_plan(registry: dict, *, url: str | None, transport: str | None,
             command: list[str] | None, env: dict[str, str], headers: dict[str, str],
             to_names: list[str]) -> tuple[dict, list[str]]:
    """The first half of ``swe mcp add``: the server's canonical definition
    and the harnesses it goes to, checked. Exactly one of ``url`` and
    ``command`` (the command and its arguments). Raises AddRefused."""
    if (url is None) == (not command):
        raise AddRefused("Give exactly one of --url=<url> or `-- <command> [args…]`.")
    if url is not None:
        if env:
            raise AddRefused("--env only applies to a command server; use --header with --url.")
        if transport not in (None, "http", "sse"):
            raise AddRefused("--transport must be http or sse.")
    else:
        if headers:
            raise AddRefused("--header only applies to a --url server.")
        if transport is not None:
            raise AddRefused("--transport only applies to a --url server.")

    targets: list[str] = []
    for place in to_names:
        if is_hub(place):
            continue  # the hub is always written
        resolved = resolve_tool_arg(registry, place)
        if _unsafe_registry_name(resolved):
            raise AddRefused(f"Unsafe harness name in registry: {place}", alarming=True)
        if not resolved:
            raise AddRefused(f"Unknown target tool: {place}")
        if get_tool_saver(resolved) is None:
            raise AddRefused(f"No MCP backend registered for {resolved}", alarming=True)
        if resolved not in targets:
            targets.append(resolved)

    if url is not None:
        canonical: dict = {"url": url}
        if transport == "sse":
            canonical["type"] = "sse"
        if headers:
            canonical["headers"] = headers
    else:
        canonical = {"command": command[0]}
        if len(command) > 1:
            canonical["args"] = command[1:]
        if env:
            canonical["env"] = env

    # A config that only launches local servers can't hold a URL one.
    if url is not None:
        local_only = [t for t in targets if not takes_remote(t)]
        if local_only:
            raise AddRefused(f"{', '.join(local_only)} {LOCAL_ONLY_NOTE}.", alarming=True)

    # A format with no SSE marker can't be given one: refuse the target
    # rather than silently downgrade the transport to http.
    if canonical.get("type") == "sse":
        incapable = [t for t in targets
                     if get_tool_format(t) in ("opencode",)
                     or get_tool_config(t).get("io_type") == "toml_region"]
        if incapable:
            raise AddRefused(f"{', '.join(incapable)} can't use an SSE server.", alarming=True)
    return canonical, targets


def add_conflicts(name: str, targets: list[str]) -> list[str]:
    """Where a server called ``name`` already is, among the hub ("swe")
    and ``targets``: what ``--force`` would replace."""
    conflicts = [HUB_LABEL] if name in get_hub_servers() else []
    return conflicts + [t for t in targets if name in get_tool_servers(t)]


def add_server(name: str, canonical: dict, targets: list[str], *, force: bool = False) -> dict:
    """The second half of ``swe mcp add``: write ``canonical`` to the hub
    and each target. Every conflict and missing secret is found before
    anything is written. Returns ``{"name", "hub", "targets"}``, the
    ``--json`` report. Raises AddRefused."""
    # What the hub stores; values already in the secrets file become ${NAME}.
    hub_entry = redact_secrets(canonical)

    hub = get_hub_servers()
    conflicts = [HUB_LABEL] if name in hub else []
    target_servers = {t: get_tool_servers(t) for t in targets}
    conflicts += [t for t in targets if name in target_servers[t]]
    if conflicts and not force:
        raise AddRefused(f"{name} already exists in: {', '.join(conflicts)}. "
                         "Re-run with --force to replace.", alarming=True)

    # A ${NAME} a harness can't satisfy must not be written as the literal.
    if targets:
        missing = unresolved_names(canonical, load_secrets())
        if missing:
            raise AddRefused("\n".join(
                f"Not adding: no value for {var} (used by {name}). "
                f"Set it with: swe mcp secret set {var}" for var in missing))

    hub_status = "replaced" if name in hub else "added"
    hub[name] = hub_entry
    save_hub_servers(hub)

    results = []
    resolved_entry = resolve_secrets(canonical)
    for target in targets:
        converted = convert_server_for_target(resolved_entry, HUB_LABEL, target)
        servers = target_servers[target]
        status = "replaced" if name in servers else "added"
        servers[name] = (_merge_format_keys(servers[name], converted)
                         if status == "replaced" else converted)
        get_tool_saver(target)(servers, get_tool_config(target)["path"])
        results.append({"name": target, "status": status})
    return {"name": name, "hub": hub_status, "targets": results}


class RemoveRefused(ValueError):
    """A --from place swe can't remove from; ``alarming`` prints in red."""

    def __init__(self, message: str, alarming: bool = False):
        super().__init__(message)
        self.alarming = alarming


def remove_places(registry: dict, name: str, from_names: list[str]) -> list[str]:
    """Where ``swe mcp remove <name> --from=…`` removes from: "swe" for
    the hub, else harness names, in order and once each. ``all`` is the hub
    plus every harness whose config names the server. Raises RemoveRefused
    for a place it can't touch."""
    places: list[str] = []
    if any(p.lower() == "all" for p in from_names):
        # The hub plus every harness whose config names the server.
        places.append(HUB_LABEL)
        for tool in get_mcp_tools(registry):
            if name in get_tool_servers(tool):
                places.append(tool)
        return places
    for place in from_names:
        if is_hub(place):
            resolved = HUB_LABEL
        else:
            resolved = resolve_tool_arg(registry, place)
            if _unsafe_registry_name(resolved):
                raise RemoveRefused(f"Unsafe harness name in registry: {place}", alarming=True)
            if not resolved:
                raise RemoveRefused(f"Unknown place: {place}")
            if resolved != HUB_LABEL and get_tool_saver(resolved) is None:
                raise RemoveRefused(f"No MCP backend registered for {resolved}", alarming=True)
        if resolved not in places:
            places.append(resolved)
    return places


def remove_server(name: str, places: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Delete ``name`` from each place's config; the body of ``swe mcp
    remove``. Returns (removed, absent, failed): where it was taken out,
    where it wasn't to begin with, and where the delete could not be
    written — a config swe cannot save stops nothing else, it is warned
    about and the rest still go.

    Taking the hub entry away also drops the server's ``swe``-block state:
    disabled marks, kept copies and provenance refer to a hub entry that
    is gone, and a kept copy could resurrect it on ``mcp enable``.
    """
    removed: list[str] = []
    absent: list[str] = []
    failed: list[str] = []
    for place in places:
        try:
            if place == HUB_LABEL:
                hub = get_hub_servers()
                if name in hub:
                    del hub[name]
                    save_hub_servers(hub)
                    removed.append(HUB_LABEL)
                else:
                    absent.append(HUB_LABEL)
                # The hub was asked either way: state left from an older
                # removal or a hand edit — a kept disabled copy, an
                # adoption record — still names this server, and mcp
                # enable could resurrect it from the kept copy. A purge
                # failure is only warned about: the entry itself is gone
                # or was never there, so the remove still stands.
                try:
                    _purge_server_state(name)
                except (OSError, ConfigurationError) as exc:
                    print(c("yellow", f"  {place}: {name} is out but its "
                                    f"state could not be cleaned ({exc})"),
                          file=sys.stderr)
                continue
            servers = get_tool_servers(place)
            if name not in servers:
                absent.append(place)
                continue
            del servers[name]
            get_tool_saver(place)(servers, get_tool_config(place)["path"])
            removed.append(place)
        except Exception as exc:
            failed.append(place)
            print(c("yellow", f"  {place}: could not remove {name} "
                            f"({exc.__class__.__name__}: {exc})"),
                  file=sys.stderr)
    return removed, absent, failed


def cmd_remove(args):
    args, json_view = take_json_flag(list(args))
    if args and args[0] in ("-h", "--help", "help"):
        print(MCP_HELP["remove"])
        return 0
    refusal = error_stream(json_view)
    try:
        args = expand_value_flags(args, {"--from"})
    except ValueError as exc:
        print(c("red", str(exc)), file=refusal)
        return 1

    positional: list[str] = []
    from_names: list[str] = []
    from_seen = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--from":
            from_seen = True
            from_names += [s for s in args[i + 1].split(",") if s]
            i += 2
        elif arg.startswith("--"):
            print(f"Unknown flag: {arg}", file=refusal)
            return 1
        else:
            positional.append(arg)
            i += 1

    # Remove accepts any existing key: names like "my server" or "_local"
    # are legal in a config even though `add` wouldn't mint them.
    if len(positional) != 1 or not positional[0]:
        print(f"Usage: {c('cyan', 'swe mcp remove <name> [--from=<place>[,<place>…]]')}",
              file=refusal)
        return 1
    name = positional[0]
    if from_seen and not from_names:
        # An empty --from asked for nowhere, not everywhere — a script
        # expanding an empty list must not delete every copy.
        print(c("red", "--from needs at least one place; "
                        "drop it to remove everywhere"), file=refusal)
        return 1
    if not from_seen:
        # Gone everywhere is the default: a copy left in a harness file is
        # adopted straight back into the hub by the next sync.
        from_names = ["all"]

    registry = load_registry()
    try:
        places = remove_places(registry, name, from_names)
    except RemoveRefused as exc:
        print(c("red", str(exc)) if exc.alarming else str(exc), file=refusal)
        return 1
    removed, absent, failed = remove_server(name, places)

    if HUB_LABEL in removed:
        # The hub entry is gone; anything still sitting in a harness file
        # comes back at the next sync's adoption pass, so say so.
        still = [t for t in get_mcp_tools(registry)
                 if name in get_tool_servers(t)]
        if still:
            print(c("yellow", f"  {name} is still in {', '.join(still)} — "
                              "the next sync will adopt it back into the hub"),
                  file=refusal)

    if json_view:
        code = emit({"name": name, "removed": removed, "absent": absent,
                     "failed": failed})
    else:
        if removed:
            print(f"Removed {name} from: {', '.join(removed)}")
        if absent:
            print(f"{name} not in: {', '.join(absent)}")
        code = 0
    return code if removed else 1


def cmd_secret(args):
    args, json_view = take_json_flag(list(args))
    if args and args[0] in ("-h", "--help", "help"):
        print(MCP_HELP["secret"])
        return 0
    # Under --json stdout is one document, so refusals go to stderr.
    refusal = error_stream(json_view)
    if args and args[0] == "remove":
        return _secret_remove(args[1:], json_view, refusal)
    if not args or args[0] != "set":
        if args:
            print(f"Unknown secret command: {args[0]}", file=refusal)
        print(f"Usage: {c('cyan', 'swe mcp secret set NAME')} | "
              f"{c('cyan', 'swe mcp secret remove NAME')}", file=refusal)
        return 1
    rest = args[1:]
    if len(rest) != 1 or rest[0].startswith("-"):
        print(f"Usage: {c('cyan', 'swe mcp secret set NAME')}", file=refusal)
        print("The value is read from stdin, never from a flag: "
              "argv shows up in `ps`.", file=refusal)
        return 1
    name = rest[0]
    if sys.stdin.isatty():
        print(f"Paste the value for {name}, then press Enter.", file=sys.stderr)
        value = sys.stdin.readline()
    else:
        value = sys.stdin.read()
    if value.endswith("\n"):
        value = value[:-1]
    # Whether the name already held a value, so a caller that stored it
    # for a step that then failed (the app's Add server) knows whether
    # removing it again restores the store.
    replaced = name in load_secrets()
    try:
        path, created = set_secret(name, value)
    except ValueError as exc:
        print(c("red", str(exc)), file=refusal)
        return 1
    if json_view:
        return emit({"name": name, "path": str(path), "created": created,
                     "replaced": replaced})
    print(f"Saved {name} to {path}")
    return 0


def _secret_remove(rest, json_view, refusal):
    """``swe mcp secret remove NAME``: drop a stored value."""
    if len(rest) != 1 or rest[0].startswith("-"):
        print(f"Usage: {c('cyan', 'swe mcp secret remove NAME')}", file=refusal)
        return 1
    name = rest[0]
    try:
        removed = remove_secret(name)
    except ValueError as exc:
        print(c("red", str(exc)), file=refusal)
        return 1
    if json_view:
        return emit({"name": name, "removed": removed})
    print(f"Removed {name}" if removed else f"No value stored for {name}")
    return 0


def cmd_diff(args):
    if args and args[0] in ("-h", "--help", "help"):
        print(MCP_HELP["diff"])
        return 0
    registry = load_registry()

    if len(args) < 2:
        print("Usage: swe mcp diff <tool1> <tool2>")
        return 1

    t1 = resolve_tool_arg(registry, args[0])
    t2 = resolve_tool_arg(registry, args[1])
    for t, a in [(t1, args[0]), (t2, args[1])]:
        if _unsafe_registry_name(t):
            print(c("red", f"Unsafe harness name in registry: {a}"))
            return 1
        if not t:
            print(f"Unknown tool: {a}")
            return 1

    s1 = get_tool_servers(t1)
    s2 = get_tool_servers(t2)
    all_names = sorted(set(s1.keys()) | set(s2.keys()))

    if not all_names:
        print("No servers in either tool.")
        return 0

    width = max(len(n) for n in all_names)
    print(f"{'SERVER':<{width}}  {t1:^12}  {t2:^12}  DIFF")
    print("─" * (width + 40))

    only1, only2, both, different = [], [], [], []
    for name in all_names:
        in1, in2 = name in s1, name in s2
        shown = sanitize(name)
        if in1 and not in2:
            only1.append(name)
            print(f"{shown:<{width}}  {cpad('green', '✓', 12)}  {cpad('dim', '—', 12)}  only in {t1}")
        elif in2 and not in1:
            only2.append(name)
            print(f"{shown:<{width}}  {cpad('dim', '—', 12)}  {cpad('green', '✓', 12)}  only in {t2}")
        else:
            both.append(name)
            if s1[name] == s2[name]:
                print(f"{shown:<{width}}  {cpad('green', '✓', 12)}  {cpad('green', '✓', 12)}  identical")
            else:
                different.append(name)
                print(f"{shown:<{width}}  {cpad('green', '✓', 12)}  {cpad('green', '✓', 12)}  {c('yellow', 'DIFFERENT')}")

    print(f"\nOnly in {t1}: {c('cyan', str(len(only1)))}")
    print(f"Only in {t2}: {c('cyan', str(len(only2)))}")
    print(f"In both: {c('cyan', str(len(both)))} ({c('yellow', str(len(different)))} differ)")
    return 0


def cmd_edit(args):
    if args and args[0] in ("-h", "--help", "help"):
        print(MCP_HELP["edit"])
        return 0
    if len(args) < 2:
        print("Usage: swe mcp edit <tool> <name>")
        return 1

    registry = load_registry()
    tool = resolve_tool_arg(registry, args[0])
    if _unsafe_registry_name(tool):
        print(c("red", f"Unsafe harness name in registry: {args[0]}"))
        return 1
    if not tool:
        print(f"Unknown tool: {args[0]}")
        return 1

    name = args[1]
    tool_cfg = get_tool_config(tool)
    if not tool_cfg:
        print(f"No MCP backend registered for {tool}.")
        return 1
    loader = get_tool_loader(tool)
    saver = get_tool_saver(tool)
    if loader is None or saver is None:
        print(f"No MCP backend registered for {tool}.")
        return 1

    tool_servers = loader(tool_cfg["path"])
    if name not in tool_servers:
        print(f"'{sanitize(name)}' not found in {sanitize(tool)}.")
        return 1

    editor = os.environ.get("EDITOR", "vim")
    # mkstemp: unpredictable name, mode 0600, O_EXCL. The old fixed name in
    # CONFIG_DIR followed a planted symlink, inherited a world-readable
    # umask, survived a crashed editor, and sat inside the versioned
    # ~/.swe tree — a resolved-secret file one `git add -A` from a
    # commit.
    fd, tmp_name = tempfile.mkstemp(prefix="swe-mcp-edit-", suffix=".json")
    tmp_file = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({name: tool_servers[name]}, indent=2) + "\n")

        try:
            subprocess.run(shlex.split(editor) + [str(tmp_file)], check=False)
        except OSError as exc:
            print(c("red", f"Editor failed ({editor}): {exc}"))
            return 1

        try:
            edited = json.loads(tmp_file.read_text())
        except (json.JSONDecodeError, UnicodeError, RecursionError):
            print("Invalid JSON — no changes saved.")
            return 0
        if not isinstance(edited, dict):
            # A JSON array answers ``name in edited`` by element membership,
            # so edited[name] then indexes a list with a string.
            print("The edit must be a JSON object — no changes saved.")
            return 0
        if name in edited and isinstance(edited[name], dict):
            # The temp file holds the server's raw tool-native dict, so it is
            # written back verbatim. Routing it through emit() treated raw
            # fields as canonical and corrupted entries on every save:
            # opencode's command list was nested one level deeper and lost
            # `environment`/`enabled`, claude's `disabled`/`autoApprove`
            # were stripped (a disabled server was re-enabled by an edit),
            # droid's transport type could be rewritten. Format conversion
            # is sync's job; a no-change save must be a no-op.
            tool_servers[name] = edited[name]
            saver(tool_servers, tool_cfg["path"])
            print(f"Updated '{sanitize(name)}' in {sanitize(tool)}")
        else:
            print("Server name removed from edit — no changes.")
        return 0
    finally:
        tmp_file.unlink(missing_ok=True)


def cmd_validate(args):
    if args and args[0] in ("-h", "--help", "help"):
        print(MCP_HELP["validate"])
        return 0

    registry = load_registry()
    mcp_tools = get_mcp_tools(registry)

    targets = []
    if args:
        for a in args:
            t = resolve_tool_arg(registry, a)
            if _unsafe_registry_name(t):
                print(c("red", f"Unsafe harness name in registry: {a}"))
                return 1
            if not t:
                print(f"Unknown tool: {a}")
                return 1
            if t not in targets:
                targets.append(t)
    else:
        targets = list(_display_tools(mcp_tools).keys())

    if not targets:
        print("No MCP-capable tools found.")
        return 1

    total_servers = 0
    total_errors = 0

    for tool in targets:
        fmt = get_tool_format(tool)
        handler = get_mcp_handler(tool)
        servers = get_tool_servers(tool)

        print(f"{tool} ({fmt})")
        if not servers:
            print(f"  {c('dim', '— no servers configured')}")
            print()
            continue

        for name, raw_cfg in sorted(servers.items()):
            total_servers += 1
            issues = []

            if not isinstance(raw_cfg, dict):
                issues.append("server config is not an object")
            else:
                canonical = handler.parse(raw_cfg)
                if raw_cfg and not canonical:
                    issues.append("could not parse tool format")
                issues.extend(get_conversion_issues(raw_cfg, fmt, fmt))

            # de-duplicate while preserving order
            seen = set()
            deduped = []
            for i in issues:
                if i not in seen:
                    seen.add(i)
                    deduped.append(i)

            if deduped:
                total_errors += 1
                print(f"  {c('red', '✗')} {sanitize(name)}: {c('yellow', '; '.join(deduped))}")
            else:
                print(f"  {c('green', '✓')} {sanitize(name)}")
        print()

    if total_errors:
        print(c("red", f"Validation failed: {total_errors}/{total_servers} server entries have issues."))
        return 1

    print(c("green", f"Validation passed: {total_servers} server entries checked."))
    return 0


def cmd_doctor(args):
    if args and args[0] in ("-h", "--help", "help"):
        print(MCP_HELP["doctor"])
        return 0

    unknown = [a for a in args if a != "--strict"]
    if unknown:
        print(f"Unknown arg(s): {', '.join(unknown)}")
        return 1

    strict = "--strict" in args

    registry = load_registry()
    mcp_tools = get_mcp_tools(registry)

    print("MCP Doctor — diagnosing all configured servers\n")

    failures = []
    seen: dict[str, str] = {}

    for tool_name, tool_cfg in mcp_tools.items():
        if not tool_cfg["path"].exists():
            continue
        servers = get_tool_servers(tool_name)
        if not servers:
            continue
        print(f"{sanitize(tool_name)} ({sanitize(tool_cfg['label'])})")
        for name, server_cfg in servers.items():
            health = check_server_health(name, server_cfg)
            st = server_type(server_cfg)
            summary = server_summary(server_cfg)
            print(f"  {health} {sanitize(name)} ({st}): {summary}")
            seen.setdefault(name, tool_name)

            plain_health = strip_ansi(health).strip()
            if plain_health != "✓":
                failures.append((tool_name, name, plain_health))
        print()

    # Harness-only servers the next sync adopts into the hub — said here
    # because the mark that scopes them lands silently otherwise.
    pending = sorted(n for n in seen if n not in get_hub_servers())
    if pending:
        print(c("dim", f"{len(pending)} server(s) not in the hub — the next "
                      f"`swe mcp sync` adopts them, held off where they are "
                      f"absent: {', '.join(pending[:8])}"
                      f"{'…' if len(pending) > 8 else ''}"))

    if strict and failures:
        print(c("red", f"Doctor strict failed: {len(failures)} unhealthy server(s)."))
        for tool_name, name, reason in failures[:20]:
            print(f"  {c('cyan', sanitize(tool_name))}:{sanitize(name)} -> {c('yellow', reason)}")
        if len(failures) > 20:
            print(f"  ... and {len(failures) - 20} more")
        return 1

    return 0


MCP_HELP = {
    "discover": f"""\
  {c('cyan', 'swe mcp discover')}              List MCP servers in tools not in mcp.json (dry-run)
  {c('cyan', 'swe mcp discover --apply')}      Add discoveries to ~/.swe/mcp.json
  {c('cyan', 'swe mcp discover --json')}       Machine-readable output
  {c('cyan', 'swe mcp discover --all')}        Include servers already in source-of-truth
  {c('cyan', 'swe mcp discover --prune')}      Also remove hub servers no harness configures

  A new pull is adopted scoped: swe.adopted_from records where it came
  from, and it is marked off for every harness not already holding it,
  so a later hub sync does not spread it. A sync's adoption pass keeps
  mcp.json current the same way, without --apply.

{c('bold', 'Examples')}
  swe mcp discover
  swe mcp discover --apply""",

    "list": f"""\
  {c('cyan', 'swe mcp list')}                 Matrix of all MCP servers across all tools
  {c('cyan', 'swe mcp list <tool>')}          Show servers for one tool only
  {c('cyan', 'swe mcp list --json')}          One JSON document, one entry per server name

{c('bold', 'Args')}
  <tool>              Tool name or alias (e.g. cc, cs, oc)

{c('bold', 'JSON')}
  Each server carries name, transport (stdio, http, sse, wrapped, unknown),
  command, args, url, env_keys, header_keys, the harnesses it appears in,
  and conflict (true when those harnesses define it differently). Env and
  header values are never printed, only their names; credentials in a URL
  or argument are hidden.

{c('bold', 'Examples')}
  swe mcp list
  swe mcp list cc
  swe mcp list --json""",

    "status": f"""\
  {c('cyan', 'swe mcp status')}               Matrix with health checks for every server
  {c('cyan', 'swe mcp status <tool>')}        Health check for one tool only
  {c('cyan', 'swe mcp status --json')}        Same fields as list --json, plus health and detail

  --json health is ok, error or unknown; detail says what failed (binary
  not found, missing env: NAME, url unreachable) and is null when healthy.

{c('bold', 'Health indicators')}
  {c('green', '✓')}   Server binary found / URL reachable
  {c('red', '✗')}   Issue detected (binary missing, URL down, env var empty)

{c('bold', 'Args')}
  <tool>              Tool name or alias (e.g. cc, cs, oc)

{c('bold', 'Examples')}
  swe mcp status
  swe mcp status oc""",

    "sync": f"""\
  {c('cyan', 'swe mcp sync <source> <target...>')}   Copy MCP servers source → target(s)
  {c('cyan', 'swe mcp sync <source> --all')}          Copy MCP servers source → all other MCP tools
  {c('cyan', 'swe mcp sync swe <target...>')}         Copy from the hub (~/.swe/mcp.json)
  {c('cyan', 'swe mcp sync swe --all')}               Push the hub out to every MCP tool

{c('bold', 'The hub')}
  ~/.swe/mcp.json is the middleman. Everything flows through it:

    harness configs  --( sync's adoption / discover --apply )-->  mcp.json
    mcp.json         --( swe mcp sync swe ... )                -->  harness configs

  It stores canonical shape, so it converts cleanly to any tool format.
  Names: {c('cyan', 'swe')}, {c('cyan', 'hub')}, {c('cyan', 'mcp.json')}, {c('cyan', '.')}
  ({c('cyan', 'quiver')} still works from before the rename.)
  Source only — a sync already pulls harness-only servers into it,
  so naming it as a target is refused.

{c('bold', 'Adoption')}
  At the end of a run, any server a harness config holds but the hub does
  not is adopted into mcp.json — first holder in sorted harness order is
  the donor when definitions disagree, provenance lands in swe.adopted_from,
  and the server is marked off for every harness not already holding it,
  so a later hub sync never pushes it anywhere it never lived. Enable it
  for a harness deliberately with: swe mcp enable NAME --for=<harness>

{c('bold', 'Flags')}
  --only=a,b,...      Copy only servers matching these names or globs
  --except=a,b,...    Skip servers matching these names or globs
  --force             Overwrite existing servers without prompting
  --skip-conflicts    Keep existing target servers on conflict
  --no-interactive    Disable interactive picker (select all)
  --no-adopt          Skip the harness-only-server adoption pass
  --dry-run           Preview changes without writing files
  --strict            Fail if conversion would be lossy
  --prune             Also delete target servers outside the selection.
                      With --only/--except or a picker deselection the
                      delete stays inside the source's own names: servers
                      it never offered are the target's own and survive.
  --json              Print what moved as one JSON document (implies
                      --no-interactive); names only, never configs

{c('bold', 'Conflict resolution')}
  Interactive mode:
    - server picker starts unchecked
    - servers already present in target(s) start checked
    - pick conflicting servers to overwrite

  Non-interactive mode:
    - all eligible servers are selected
    - conflicts are skipped unless --force is used

  Strict mode:
    - aborts before writing if any selected server loses/changes canonical fields

{c('bold', 'Examples')}
  swe mcp sync oc cursor
  swe mcp sync opencode --all
  swe mcp sync opencode droid
  swe mcp sync opencode myagent     # unverified target → ~/.myagent/mcp.json
  swe mcp sync swe --all --only='*__*'              # hub → everything
  swe mcp sync swe cursor --only=dv__*              # hub → one tool
  swe mcp sync opencode cursor --only=dv__github,dv__linear
  swe mcp sync opencode cursor --only=dv__*            # one category
  swe mcp sync opencode --all --only='*__*'            # every prefixed server
  swe mcp sync opencode --all --except=computer-use,node_repl
  swe mcp sync opencode cursor --force
  swe mcp sync opencode cursor --no-interactive --skip-conflicts
  swe mcp sync opencode cursor --dry-run
  swe mcp sync opencode cursor --strict""",

    "diff": f"""\
  {c('cyan', 'swe mcp diff <tool1> <tool2>')}  Compare MCP configs between two tools

{c('bold', 'Args')}
  <tool1> <tool2>     Tool names or aliases to compare

{c('bold', 'Examples')}
  swe mcp diff claude cursor
  swe mcp diff cc cs""",

    "edit": f"""\
  {c('cyan', 'swe mcp edit <tool> <name>')}    Edit one server config in one tool

  Opens the server config JSON in your editor. Save and exit to apply.
  If you remove the server key from the file, no changes are saved.
  The edited JSON REPLACES the full server config, so copy any unusual
  keys (e.g. codex-specific `startup_timeout_sec = 120`, `cwd`, etc.)
  verbatim or they'll be lost on save.

{c('bold', 'Args')}
  <tool>              Tool name or alias (e.g. oc, cc, cursor, codex)
  <name>              Server name in that tool's config

{c('bold', 'Editor')}
  Uses $EDITOR env var, falls back to vim.
  $EDITOR can be a multi-word command (e.g. `code --wait`) — it is shlex-split.

{c('bold', 'Examples')}
  swe mcp edit opencode dv__github
  EDITOR=code swe mcp edit cursor GitKraken
  swe mcp edit codex node_repl     # preserve startup_timeout_sec verbatim""",

    "validate": f"""\
  {c('cyan', 'swe mcp validate')}             Validate all MCP-capable tools
  {c('cyan', 'swe mcp validate <tool...>')}    Validate one or more tools

  Validation checks:
    - server entry is a JSON object
    - tool-specific parser can parse it
    - same-format roundtrip does not lose canonical fields

{c('bold', 'Examples')}
  swe mcp validate
  swe mcp validate opencode
  swe mcp validate oc cc""",

    "add": f"""\
  {c('cyan', 'swe mcp add <name> -- <command> [args…]')}     Add a stdio server
  {c('cyan', 'swe mcp add <name> --url=<url>')}              Add an http server

  The hub (~/.swe/mcp.json) is always written; --to also writes the
  given harnesses in their own format. --env goes with a command,
  --header with a URL. A ${'{'}NAME{'}'} value stays a reference in the
  hub and resolves when a harness is written — a name with no value in
  the secrets file refuses before anything is written.

{c('bold', 'Flags')}
  --to=a,b            Also write these harnesses
  --env=K=V           Env pair for a command server (repeatable)
  --header=K=V        Header pair for a URL server (repeatable)
  --transport=sse     Legacy SSE instead of streamable http
  --force             Replace an existing server of the same name
  --json              Print the result as one JSON document

{c('bold', 'Examples')}
  swe mcp add github -- npx -y @modelcontextprotocol/server-github
  swe mcp add notion --env=NOTION_TOKEN='${'{'}NOTION_TOKEN{'}'}' -- npx -y @notionhq/notion-mcp-server
  swe mcp add remote --url=https://mcp.example.com/sse --transport=sse \\
      --header='Authorization=Bearer ${'{'}MCP_TOKEN{'}'}' --to=claude,codex""",

    "remove": f"""\
  {c('cyan', 'swe mcp remove <name>')}                          Remove a server everywhere
  {c('cyan', 'swe mcp remove <name> --from=<place>[,<place>…]')}   Remove from given places

  With no --from, the server goes from the hub and every harness file
  holding it — the only shape that stays gone, since a copy left in a
  file is adopted straight back into the hub by the next sync. Its
  disabled marks and kept copies go with the hub entry.

  Places are harness names, the hub (~/.swe/mcp.json), or
  {c('cyan', 'all')} (the hub plus every harness whose config names the server).
  Places that don't have the server are reported absent; a file swe
  cannot write is warned about and skipped. Removing nothing exits 1.

{c('bold', 'Flags')}
  --from=a,b          Where to remove from (default: all)
  --json              Print the result as one JSON document

{c('bold', 'Examples')}
  swe mcp remove github                 # gone from the hub and every file
  swe mcp remove github --from=swe      # hub only — warns what stays
  swe mcp remove github --from=claude,codex""",

    "disable": f"""\
  {c('cyan', 'swe mcp disable <name> [--for=h1,h2]')}   Switch a server off per harness

  The entry leaves the harness config and the hub keeps a copy, so it
  toggles back on without losing anything. No ``--for`` means every
  harness that defines it. This is swe's own state in mcp.json, not a
  format flag, so it works the same for every harness — and ``sync``
  honours it: a sync cannot silently turn a disabled server back on.

  A bare disable is the master switch: the harnesses it turns off are
  recorded in mcp.json's ``swe.master_off`` so a bare ``enable`` restores
  exactly them — never a harness you had already switched off by hand.
  A ``--for`` disable leaves that restore set.

{c('bold', 'Flags')}
  --for=a,b         Only these harnesses (default: all that define it)
  --json            Print the result as one JSON document

{c('bold', 'Examples')}
  swe mcp disable github
  swe mcp disable github --for=cursor""",

    "enable": f"""\
  {c('cyan', 'swe mcp enable <name> [--for=h1,h2]')}    Switch a server back on

  Restores what ``disable`` kept: the harness's own entry when one was
  stored (per-format keys and this harness's variant survive the toggle),
  else the hub's canonical copy. No ``--for`` means every harness the
  server is marked off for.

  When a bare ``disable`` recorded ``swe.master_off`` for the server, a
  bare enable restores exactly that set — a harness you switched off by
  hand before the master-off stays off. ``--for`` enables just the named
  harnesses and takes them out of any recorded restore set.

{c('bold', 'Flags')}
  --for=a,b         Only these harnesses
  --json            Print the result as one JSON document

{c('bold', 'Examples')}
  swe mcp enable github
  swe mcp enable github --for=cursor,codex""",

    "set": f"""\
  {c('cyan', 'swe mcp set <name> --url=<url>')}            Point a server at a new URL
  {c('cyan', 'swe mcp set <name> -- <command> [args…]')}   Give it a new command
  {c('cyan', 'swe mcp set <name> --env=K=V')}              Add or replace one env pair

  Changes the hub entry (~/.swe/mcp.json) in place, then writes the new
  definition to every harness that already holds the server. A harness
  where the server is off, or was never synced, is left alone. Only the
  fields you name change: other env and header pairs, the shared flag
  and the type label stay. A new URL replaces a command and the other
  way round. A ${{NAME}} with no value in the secrets file refuses before
  anything is written.

{c('bold', 'Flags')}
  --url=<url>         The server's new URL
  --env=K=V           Add or replace an env pair (repeatable)
  --unset-env=K       Drop an env pair (repeatable)
  --header=K=V        Add or replace a header pair (repeatable)
  --unset-header=K    Drop a header pair (repeatable)
  --json              Print the result as one JSON document

{c('bold', 'Examples')}
  swe mcp set docs --url=https://mcp.example.com:8108/api/mcp
  swe mcp set github --env=GITHUB_TOKEN='${{GITHUB_TOKEN}}'
  swe mcp set github -- npx -y @modelcontextprotocol/server-github""",
    "rename": f"""\
  {c('cyan', 'swe mcp rename <old> <new>')}           Rename a server everywhere

  Moves the hub entry, the off switches and kept copies recorded for it,
  and the entry in every harness config that holds it. Each harness keeps
  its own definition; only the name changes. A shared server's gateway
  URL follows the new name. Refuses when <new> is already taken in the
  hub or in any harness.

{c('bold', 'Flags')}
  --json              Print the result as one JSON document

{c('bold', 'Examples')}
  swe mcp rename design-systems constellations""",
    "set-type": f"""\
  {c('cyan', 'swe mcp set-type <name> <type>')}       Label a hub server
  {c('cyan', 'swe mcp set-type <name> --clear')}      Remove the label

  The label lives on the server's entry in ~/.swe/mcp.json — swe-side
  metadata like ``shared``, never written into a harness config, and
  carried as ``type`` on ``mcp list`` / ``mcp info --json`` rows.
  Expected values: {', '.join(gateway_defaults.EXPECTED_TYPES)} — but any
  word works. The value is trimmed, and an empty one clears too.

  Reserved: the transport words ({', '.join(sorted(gateway_defaults.TRANSPORT_TYPES))})
  are the wire marker ``type`` also carries — a URL server's ``sse`` —
  so they can never be labels, and set-type refuses to overwrite a live
  marker (relabel an SSE server by editing mcp.json).

{c('bold', 'Flags')}
  --clear           Remove the label
  --json            Print the result as one JSON document

{c('bold', 'Examples')}
  swe mcp set-type github dev
  swe mcp set-type github --clear""",

    "info": f"""\
  {c('cyan', 'swe mcp info <name>')}           One server's full detail
  {c('cyan', 'swe mcp info <name> --json')}    Machine-readable detail

  Everything swe knows about one server: its scrubbed definition
  (command, args, url, env and header key names — never values), which
  harnesses hold it, where it is switched off, the files that define it
  with each file's last-modified time, missing ${'{'}NAME{'}'} secrets,
  the health check ``mcp status`` runs, and the live gateway's row when
  the server is shared.

{c('bold', 'JSON')}
  Adds to the ``mcp list`` row: ``shared``, ``places`` (place, path,
  state on/off/hub, updated), ``health``, ``health_detail``, ``updated``
  (the newest file mtime) and ``gateway`` (state, pid, uptime_s, starts,
  error, calls, calls_by_tool, call_errors, last_call — null when the
  gateway is not running).

{c('bold', 'Examples')}
  swe mcp info github
  swe mcp info github --json""",

    "tools": f"""\
  {c('cyan', 'swe mcp tools <name>')}          List a server's tools by live handshake
  {c('cyan', 'swe mcp tools <name> --json')}   Machine-readable tool list

  Runs the MCP initialize + tools/list exchange and prints what the
  server offers. A command server is spawned for it and killed after; a
  server at a URL is asked over streamable http with its configured
  headers. The definition the probe runs is the hub's when the hub holds
  one — a shared server's harness entry points at the gateway, not the
  server itself — with ${'{'}NAME{'}'} secrets resolved into the child
  environment or the request headers.

{c('bold', 'Flags')}
  --timeout=N         Seconds to wait for initialize and for tools/list
  --json              Print the result as one JSON document

  A redirect is refused, so the headers never reach another host. A
  server that wants a browser sign-in answers 401 and is reported as
  such, and the older sse transport is not probed.

{c('bold', 'Examples')}
  swe mcp tools github
  swe mcp tools github --json""",

    "secret": f"""\
  {c('cyan', 'swe mcp secret set NAME')}       Store a ${'{'}NAME{'}'} value in ~/.swe/secrets/mcp/.api_keys
  {c('cyan', 'swe mcp secret remove NAME')}    Drop a stored value

  mcp.json keeps ${'{'}NAME{'}'} references; the values live in the secrets
  file, mode 600 and gitignored. Sync refuses to write a harness config
  while a reference has no value.

{c('bold', 'Reading the value')}
  The value comes from stdin — never a flag, which `ps` would show:

    printf %s "$MY_TOKEN" | swe mcp secret set MY_TOKEN

  Piped input is read whole (one trailing newline is dropped); on a
  terminal it asks for one line.

{c('bold', 'Flags')}
  --json              Print the result as one JSON document

{c('bold', 'Examples')}
  swe mcp secret set EXA_API_KEY < ~/.tokens/exa
  printf %s "$TOKEN" | swe mcp secret set EXA_API_KEY
  printf %s "$TOKEN" | swe mcp secret set EXA_API_KEY --json""",

    "doctor": f"""\
  {c('cyan', 'swe mcp doctor')}               Deep diagnostics for all configured servers

  Checks every server across all tools:
    - Binary exists in PATH
    - URL is reachable
    - Required env vars are set

{c('bold', 'Flags')}
  --strict            Return non-zero if any server is unhealthy

{c('bold', 'Examples')}
  swe mcp doctor
  swe mcp doctor --strict""",

    "gateway": f"""\
  {c('cyan', 'swe mcp gateway')}                     Show the gateway and its shared servers
  {c('cyan', 'swe mcp gateway share <server...>')}    Run these hub servers once for every session
  {c('cyan', 'swe mcp gateway unshare <server...>')}  Give each session its own copy again

  Every harness session starts its own copy of each stdio server it is
  configured with. The gateway daemon starts each shared server once and
  serves it at http://127.0.0.1:{gateway_defaults.DEFAULT_PORT}/mcp/<server>; once it is
  running, `swe mcp sync swe ...` writes that URL for shared servers
  instead of the command. Claude Desktop takes local servers only, so it
  keeps the command.

  The daemon is the standalone warp-gate package
  (github.com/starship-terminal/warp-gate): `warp-gate install` puts it
  under launchd and marks mcp.json's "gateway" key enabled. swe keeps the
  manifest side — share/unshare edit "shared" — and status reads the
  daemon's /status endpoint, which warp-gate serves identically.

  Share only servers that keep no state per session (API clients such as
  GitHub, Linear, Notion or search). A browser or REPL server would hand
  every session the same page; share refuses those without --force.

{c('bold', 'Flags')}
  --force             share: share a server that looks stateful anyway
  --json              status: print one JSON document

{c('bold', 'Examples')}
  swe mcp gateway share dv__github dv__linear sr__brave
  warp-gate install
  swe mcp sync swe --all --force
  swe mcp gateway status --json""",
}

def cmd_help(args=None):
    if args:
        cmd_name = {"ls": "list"}.get(args[0], args[0])
        if cmd_name in MCP_HELP:
            print(MCP_HELP[cmd_name])
            return 0
        print(f"Unknown command: {cmd_name}")
        return 1
    print(f"""
  {c('bold', 'swe mcp')} — MCP server manager for AI coding tools

  {c('cyan', 'swe mcp discover')}     Find MCP servers across tool configs
  {c('cyan', 'swe mcp list')}         Matrix view of MCP servers across tools
  {c('cyan', 'swe mcp status')}       List with health checks
  {c('cyan', 'swe mcp sync')}         Copy servers source → target(s)
  {c('cyan', 'swe mcp diff')}         Compare two tools' configs
  {c('cyan', 'swe mcp edit')}         Edit one server config in one tool
  {c('cyan', 'swe mcp validate')}     Validate MCP config shape for one/all tools
  {c('cyan', 'swe mcp add')}          Add a server to the hub (and harnesses)
  {c('cyan', 'swe mcp remove')}       Remove a server from the hub or harnesses
  {c('cyan', 'swe mcp set')}          Change a hub server's URL, command or keys
  {c('cyan', 'swe mcp rename')}       Rename a server everywhere
  {c('cyan', 'swe mcp set-type')}     Label a hub server
  {c('cyan', 'swe mcp secret set')}   Store a ${{NAME}} value in the secrets file
  {c('cyan', 'swe mcp doctor')}       Deep diagnostics
  {c('cyan', 'swe mcp gateway')}      Shared-server flags + daemon status

{c('bold', 'Examples')}
  swe mcp list
  swe mcp sync opencode cursor
  swe mcp status oc

  {c('dim', 'swe mcp <command> help   detailed help for one command')}
""")
    return 0


def _enable_disable_targets(registry, mcp_tools, for_flag, fallback,
                            err=None) -> list[str] | None:
    """Resolve ``--for=a,b`` into harness names, or use ``fallback``.

    Prints and returns None on a bad name or the hub being named — the
    hub cannot be toggled, it is the restore source.
    """
    if for_flag is None:
        return fallback
    targets = []
    for arg in for_flag:
        resolved = resolve_tool_arg(registry, arg)
        if not resolved or is_hub(resolved):
            print(f"Unknown harness: {arg}", file=err or sys.stdout)
            return None
        targets.append(resolved)
    return targets


def _for_flag(args, json_view):
    """Shared ``--for=a,b`` parse for enable/disable: (names, positionals)
    or (None, None) with the refusal already printed."""
    refusal = error_stream(json_view)
    try:
        args = expand_value_flags(args, {"--for"})
    except ValueError as exc:
        print(c("red", str(exc)), file=refusal)
        return None, None
    for_flag: list[str] | None = None
    positional: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--for":
            for_flag = [s for s in args[i + 1].split(",") if s]
            i += 2
        elif arg.startswith("--"):
            print(f"Unknown flag: {arg}", file=refusal)
            return None, None
        else:
            positional.append(arg)
            i += 1
    return for_flag, positional


def _shielded_copy(server: dict) -> dict | None:
    """``shield_credentials`` with its notes sent to stderr, keeping
    ``--json``'s stdout a clean document."""
    shielded, notes = shield_credentials(server)
    for note in notes:
        print(c("yellow", f"  {note}"), file=sys.stderr)
    return shielded


def cmd_disable(args):
    if args and args[0] in ("-h", "--help", "help"):
        print(MCP_HELP["disable"])
        return 0
    args, json_view = take_json_flag(list(args))
    refusal = error_stream(json_view)
    for_flag, positional = _for_flag(args, json_view)
    if positional is None:
        return 1
    if not positional:
        print(f"Usage: {c('cyan', 'swe mcp disable NAME [--for=h1,h2]')}", file=refusal)
        return 1
    name = positional[0]

    registry = load_registry()
    mcp_tools = get_mcp_tools(registry)
    holding_names = [t for t in _display_tools(mcp_tools)
                     if not is_hub(t) and name in get_tool_servers(t)]
    targets = _enable_disable_targets(registry, mcp_tools, for_flag,
                                      holding_names, err=refusal)
    if targets is None:
        return 1
    if not targets:
        print(f"{name} is not defined in any harness.", file=refusal)
        return 1

    if for_flag is not None:
        # An explicit --for off leaves the restore set up front: the user
        # asked for these harnesses off, so a later master-on must not
        # resurrect them — even where nothing was left to remove.
        _drop_master_off(name, targets)

    # First the backups, then any deletion: a disabled server must always
    # be recoverable, so nothing leaves a file until the hub and the
    # per-harness raw copies are proven storable.
    holding: dict[str, dict] = {}
    shielded_raws: dict[str, dict] = {}
    for target in targets:
        servers = get_tool_servers(target)
        if name not in servers:
            if not json_view:
                print(f"  {target}: {name} is not defined — skipped")
            continue
        saver = get_tool_saver(target)
        if saver is None:
            if not json_view:
                print(f"  {target}: cannot write {target}'s config — skipped")
            continue
        shielded_raw = _shielded_copy(dict(servers[name]))
        if shielded_raw is None:
            print(f"  {target}: {name} holds a credential the secrets file "
                  f"cannot store — left alone", file=refusal)
            continue
        holding[target] = servers
        shielded_raws[target] = shielded_raw

    if not holding:
        print(f"{name} cannot be switched off anywhere.", file=refusal)
        return 1

    if name not in get_hub_servers():
        # A canonical hub copy keeps the server listed and is the restore
        # for harnesses toggled off before raw copies existed. Saved before
        # the first deletion: a write that fails loses nothing yet.
        first = next(iter(holding))
        canonical = get_mcp_handler(first).parse(holding[first][name])
        shielded = _shielded_copy(canonical)
        if shielded is None:
            print(f"The hub cannot keep a copy of {name} — left alone.",
                  file=refusal)
            return 1
        hub = get_hub_servers()
        hub[name] = shielded
        save_hub_servers(hub)
        # The stash is an adoption too: scoped to the harnesses the server
        # was found in, or a later hub sync would spread it to ones it
        # never lived in.
        state = _swe_state()
        _record_adopted(state, name, first, set(holding_names), mcp_tools)
        _save_swe_state(state)

    turned_off: list[str] = []
    for target in targets:
        servers = holding.get(target)
        if servers is None:
            continue
        del servers[name]
        get_tool_saver(target)(servers, get_tool_config(target)["path"])
        _mark_disabled(target, name, shielded_raws[target])
        turned_off.append(target)
    if turned_off and for_flag is None:
        # A bare disable is the master switch: record the harnesses it
        # switched off so a bare enable restores exactly them — never
        # a harness the user had already turned off by hand.
        _record_master_off(name, turned_off)
    if not json_view:
        for target in turned_off:
            print(f"{c('green', '✓')} {name} off for {c('cyan', target)}")
    if json_view:
        emit(ToggleReply(name, disabled_in=turned_off).to_json())
    return 0 if turned_off else 1


def cmd_enable(args):
    if args and args[0] in ("-h", "--help", "help"):
        print(MCP_HELP["enable"])
        return 0
    args, json_view = take_json_flag(list(args))
    refusal = error_stream(json_view)
    for_flag, positional = _for_flag(args, json_view)
    if positional is None:
        return 1
    if not positional:
        print(f"Usage: {c('cyan', 'swe mcp enable NAME [--for=h1,h2]')}", file=refusal)
        return 1
    name = positional[0]

    registry = load_registry()
    mcp_tools = get_mcp_tools(registry)
    recorded = master_off_map().get(name)
    fallback = [t for t, names in disabled_map().items() if name in names]
    if for_flag is None and recorded:
        # A master-on restores exactly what the master-off disabled; a
        # harness turned off by hand before it is not resurrected.
        fallback = recorded
    targets = _enable_disable_targets(registry, mcp_tools, for_flag,
                                      fallback, err=refusal)
    if targets is None:
        return 1
    if not targets:
        print(f"{name} is not disabled anywhere.", file=refusal)
        return 1

    hub = get_hub_servers()
    defs_ = disabled_defs()
    secrets = load_secrets()
    if name not in hub and not any(name in (defs_.get(t) or {}) for t in targets):
        for target in targets:
            _mark_enabled(target, name)
        # The marks went with nothing left to restore: the restore set is
        # spent too, or a later bare enable would chase a ghost.
        if for_flag is None:
            _drop_master_off(name)
        else:
            _drop_master_off(name, targets)
        print(f"The hub has no copy of {name} to restore — "
              f"cleared the disabled mark. Re-add it with `swe mcp add`.",
              file=refusal)
        return 1

    def restore(target: str) -> str | None:
        """Put the server back for one harness; None, or why it did not.

        Each path manages its own mark: a restore that cannot land leaves
        the mark (and the kept copy) in place for a later enable.
        """
        servers = get_tool_servers(target)
        if name in servers:
            # Re-added while marked off; the mark just clears.
            _mark_enabled(target, name)
            return None
        raw = (defs_.get(target) or {}).get(name)
        if raw is not None:
            # The harness's own copy, kept verbatim: per-format keys and
            # this harness's variant survive the toggle.
            restored = resolve_secrets(raw, secrets)
            missing = unresolved_names(restored, secrets)
            if missing:
                return (f"no value for {', '.join(missing)} — set it with: "
                        f"swe mcp secret set {missing[0]}")
            saver = get_tool_saver(target)
            if saver is None:
                return f"cannot write {target}'s config"
            servers[name] = restored
            saver(servers, get_tool_config(target)["path"])
            if name not in get_tool_servers(target):
                return "the entry did not reach the config"
            _mark_enabled(target, name)
            return None
        if name not in hub:
            return "no copy to restore it from"
        # Sync skips disabled names, so the mark lifts first and goes back
        # on any failure.
        _mark_enabled(target, name)
        # --json's stdout is one document: the inner sync reports to stderr.
        with redirect_stdout(sys.stderr if json_view else sys.stdout):
            code = _sync([HUB_LABEL, target, f"--only={name}",
                          "--no-interactive", "--force"], None)
        if code != 0 or name not in get_tool_servers(target):
            _mark_disabled(target, name, None)
            return "restore sync failed"
        return None

    turned_on: list[str] = []
    failed: list[str] = []
    for target in targets:
        try:
            why = restore(target)
        except Exception as exc:
            # The mark may have been lifted mid-restore; put it back.
            _mark_disabled(target, name, None)
            why = str(exc)
        if why is None:
            turned_on.append(target)
        else:
            failed.append(target)
            if not json_view:
                print(f"  {target}: {name} stays off — {why}", file=refusal)
    # Restored harnesses leave the restore set: a bare enable spends its
    # recorded set, a --for enable takes the named harnesses out of any
    # set still standing. A failed restore keeps its entry, so a retry
    # still targets exactly it rather than every marked harness.
    _drop_master_off(name, turned_on)
    if json_view:
        emit(ToggleReply(name, enabled_in=turned_on, failed_in=failed).to_json())
    return 0 if turned_on else 1


def cmd_set_type(args):
    if args and args[0] in ("-h", "--help", "help"):
        print(MCP_HELP["set-type"])
        return 0
    args, json_view = take_json_flag(list(args))
    refusal = error_stream(json_view)
    clear = "--clear" in args
    positional: list[str] = []
    for arg in args:
        if arg == "--clear":
            continue
        if arg.startswith("--"):
            print(f"Unknown flag: {arg}", file=refusal)
            return 1
        positional.append(arg)
    if len(positional) != (1 if clear else 2):
        print(f"Usage: {c('cyan', 'swe mcp set-type NAME TYPE')}  |  "
              f"{c('cyan', 'swe mcp set-type NAME --clear')}", file=refusal)
        return 1
    name = positional[0]
    # An empty or all-space value cannot label anything: it clears.
    label = None if clear else (positional[1].strip() or None)

    servers = get_hub_servers()
    entry = servers.get(name)
    if not isinstance(entry, dict):
        print(f"{name} is not in the hub — the label lives on its entry in "
              f"{MCP_SOURCE_FILE} (`swe mcp discover --apply` pulls one in).",
              file=refusal)
        return 1

    marked = gateway_defaults.is_transport_marked(entry)

    if label is not None:
        if label.lower() in gateway_defaults.TRANSPORT_TYPES:
            print(f"'{label}' is a transport marker, not a label — it "
                  f"would change how harnesses connect. Reserved: "
                  f"{', '.join(sorted(gateway_defaults.TRANSPORT_TYPES))}.",
                  file=refusal)
            return 1
        if marked:
            print(f"{name}'s type is the sse transport marker — "
                  "overwriting it would change how the server connects. "
                  f"Edit {MCP_SOURCE_FILE} by hand to relabel it anyway.",
                  file=refusal)
            return 1
        entry[gateway_defaults.TYPE_KEY] = label
        save_hub_servers(servers)
        if json_view:
            return emit(SetTypeReply(name, label).to_json())
        print(f"{name}: type {label}")
        return 0

    if marked:
        # The marker is not a label, and dropping it would downgrade the
        # server to plain http on the next sync — left alone either way.
        if json_view:
            return emit(SetTypeReply(name).to_json())
        print(f"{name}: type is the sse transport marker, not a label — "
              "left alone.")
        return 0
    changed = gateway_defaults.TYPE_KEY in entry
    entry.pop(gateway_defaults.TYPE_KEY, None)
    if changed:
        save_hub_servers(servers)
    if json_view:
        return emit(SetTypeReply(name).to_json())
    print(f"{name}: type cleared" if changed else f"{name}: no type label set")
    return 0


def cmd_set(args):
    from swe.mcp.edit_commands import cmd_set as run
    return run(args)


def cmd_rename(args):
    from swe.mcp.edit_commands import cmd_rename as run
    return run(args)


def cmd_gateway(args):
    from swe.mcp.gateway_commands import cmd_gateway as run

    return run(args)


def cmd_mcp_discover(args):
    from swe.mcp.discover_commands import cmd_discover

    return cmd_discover(args)


# -- info / tools -----------------------------------------------------


def _server_lookup(name: str) -> tuple[dict, dict] | None:
    """``(row, raw)`` for one server across every harness and the hub.

    The hub is always read so a server switched off everywhere still
    resolves — disabling leaves its copy there.
    """
    registry = load_registry()
    tool_data = {t: get_tool_servers(t)
                 for t in _display_tools(get_mcp_tools(registry))}
    for row, raw in server_inventory(tool_data, include_hub=True):
        if row["name"] == name:
            return row, raw
    return None


def _places_for(name: str) -> list[PlaceRow]:
    """Every file that does or did define ``name``, with its mtime.

    Harnesses that never held the server stay out — "missing from" is the
    matrix's job, not a file relationship. ``off`` is swe's disable mark.
    """
    disabled = disabled_map()
    hub = get_hub_servers()
    registry = load_registry()
    places = []
    for tool in sorted(_display_tools(get_mcp_tools(registry))):
        servers = get_tool_servers(tool)
        if name in servers:
            state = "on"
        elif name in (disabled.get(tool) or []):
            state = "off"
        else:
            continue
        cfg = get_tool_config(tool) or {}
        path = cfg.get("path")
        places.append(PlaceRow(tool, str(path) if path else "", state,
                               _mtime_iso(path)))
    if name in hub:
        places.append(PlaceRow(HUB_LABEL, str(MCP_SOURCE_FILE), "hub",
                               _mtime_iso(MCP_SOURCE_FILE)))
    return places


def _mtime_iso(path) -> str | None:
    try:
        mtime = Path(os.path.expanduser(str(path))).stat().st_mtime
    except (OSError, TypeError, ValueError):
        return None
    return datetime.fromtimestamp(mtime).isoformat(timespec="seconds")


def _gateway_row(name: str) -> dict | None:
    """The live gateway's ``/status`` row for ``name``, when reachable."""
    try:
        from swe.mcp.gateway_commands import gateway_status
        status = gateway_status()
    except Exception:
        return None
    if not status.get("reachable"):
        return None
    for row in status.get("servers") or []:
        if isinstance(row, dict) and row.get("name") == name:
            return row
    return None


def _canonical_for_probe(name: str, raw: dict,
                         row: dict) -> tuple[str, dict] | None:
    """The definition a probe should run: hub first, first harness else.

    A shared server's harness entry points at the gateway URL, so probing
    it would test the gateway, not the server — the hub keeps the real
    command. Secrets resolve into the copy the child process sees.
    """
    hub = get_hub_servers()
    secrets = load_secrets()
    if name in hub:
        canonical = normalize_server(resolve_secrets(hub[name], secrets))
        return "swe", canonical
    for tool in row.get("harnesses") or []:
        handler = get_mcp_handler(tool)
        raw_entry = (get_tool_servers(tool) or {}).get(name)
        if isinstance(raw_entry, dict):
            canonical = normalize_server(
                resolve_secrets(handler.parse(raw_entry), secrets))
            return tool, canonical
    handler = get_mcp_handler("swe")
    return None, normalize_server(resolve_secrets(handler.parse(raw), secrets))


def cmd_info(args):
    if args and args[0] in ("-h", "--help", "help"):
        print(MCP_HELP["info"])
        return 0
    args, json_view = take_json_flag(list(args))
    refusal = error_stream(json_view)
    positional = [a for a in args if not a.startswith("-")]
    if not positional:
        print(f"Usage: {c('cyan', 'swe mcp info NAME')}", file=refusal)
        return 1
    name = positional[0]
    found = _server_lookup(name)
    if found is None:
        print(f"Unknown server: {name}", file=refusal)
        print("Run `swe mcp list` for the names swe knows.", file=refusal)
        return 1
    row, raw = found

    hub = get_hub_servers()
    places = _places_for(name)
    health, health_detail = probe_server_health(name, raw)
    shared = gateway_defaults.is_shared(hub.get(name))
    updated = max((p.updated for p in places if p.updated), default=None)
    reply = InfoReply(
        name=name,
        transport=row["transport"],
        command=row["command"],
        args=row["args"],
        url=row["url"],
        env_keys=row["env_keys"],
        header_keys=row["header_keys"],
        secrets_missing=row["secrets_missing"],
        in_hub=row["in_hub"],
        shared=shared,
        harnesses=row["harnesses"],
        conflict=row["conflict"],
        variants=row["variants"],
        disabled_in=row["disabled_in"],
        places=places,
        health=health,
        health_detail=health_detail,
        updated=updated,
        type=gateway_defaults.label_of(hub.get(name)),
        adopted_from=adopted_map().get(name),
        gateway=_gateway_row(name),
    )
    if json_view:
        return emit(reply.to_json())

    print(f"{c('bold', name)}  {row['transport']}  {_health_badge(health)}")
    if row["command"]:
        args_str = " ".join(row["args"])
        print(f"  command   {row['command']}" + (f" {args_str}" if args_str else ""))
    if row["url"]:
        print(f"  url       {row['url']}")
    if row["env_keys"]:
        print(f"  env       {', '.join(row['env_keys'])}")
    if row["header_keys"]:
        print(f"  headers   {', '.join(row['header_keys'])}")
    if row["harnesses"]:
        print(f"In          {', '.join(row['harnesses'])}")
    if row["disabled_in"]:
        print(f"Off in      {', '.join(row['disabled_in'])}")
    hub_line = "yes" if row["in_hub"] else "no"
    if shared:
        hub_line += " · shared through the gateway"
    print(f"Hub         {hub_line}")
    if reply.adopted_from:
        print(f"Adopted     from {reply.adopted_from}")
    if reply.type:
        print(f"Type        {reply.type}")
    if row["conflict"]:
        print("Variants    " + " | ".join("+".join(v) for v in row["variants"]))
    if row["secrets_missing"]:
        print(f"Missing     {', '.join(row['secrets_missing'])} in secrets")
    if health_detail:
        print(f"Health      {health_detail}")
    for place in places:
        stamp = f"  {place.updated}" if place.updated else ""
        print(f"  {place.place:<10}  {place.state:<4} {place.path}{stamp}")
    gw = reply.gateway
    if gw:
        bits = [gw.get("state") or "?"]
        if gw.get("pid"):
            bits.append(f"pid {gw['pid']}")
        if gw.get("uptime_s") is not None:
            bits.append(f"up {gw['uptime_s']}s")
        if gw.get("starts") is not None:
            bits.append(f"starts {gw['starts']}")
        if gw.get("calls"):
            bits.append(f"{gw['calls']} calls" +
                        (f" ({gw['call_errors']} errors)" if gw.get("call_errors") else ""))
        if gw.get("last_call"):
            bits.append(f"last {gw['last_call']}")
        if gw.get("error"):
            bits.append(f"error: {gw['error']}")
        print(f"Gateway     {' · '.join(bits)}")
    return 0


def _health_badge(health: str) -> str:
    if health == "ok":
        return c("green", "✓")
    if health == "error":
        return c("red", "✗")
    return c("yellow", "?")


def cmd_tools(args):
    if args and args[0] in ("-h", "--help", "help"):
        print(MCP_HELP["tools"])
        return 0
    args, json_view = take_json_flag(list(args))
    refusal = error_stream(json_view)
    timeout = None
    positional = []
    for a in args:
        if a.startswith("--timeout="):
            try:
                timeout = float(a.split("=", 1)[1])
            except ValueError:
                print(f"Bad --timeout: {a}", file=refusal)
                return 1
        elif not a.startswith("-"):
            positional.append(a)
    if not positional:
        print(f"Usage: {c('cyan', 'swe mcp tools NAME')}", file=refusal)
        return 1
    name = positional[0]
    found = _server_lookup(name)
    if found is None:
        print(f"Unknown server: {name}", file=refusal)
        return 1
    row, raw = found
    _source, canonical = _canonical_for_probe(name, raw, row)
    st = server_type(canonical)

    def fail(msg: str) -> int:
        if json_view:
            emit(ToolsReply(name, st, error=msg).to_json())
        else:
            print(f"{name}: {msg}", file=refusal)
        return 1

    if st == "http" and str(canonical.get("type") or "").lower() == "sse":
        return fail("the tools probe speaks stdio and streamable http; "
                    "this server uses the older sse transport")
    if st not in ("stdio", "http"):
        return fail(f"the tools probe speaks stdio and http; this server is {st}")
    from swe.mcp.probe import ProbeError, inspect_http, inspect_stdio
    try:
        if st == "http":
            result = inspect_http(str(canonical.get("url") or ""),
                                  canonical.get("headers") or {},
                                  timeout_s=timeout)
        else:
            result = inspect_stdio(str(canonical.get("command") or ""),
                                   canonical.get("args") or [],
                                   canonical.get("env") or {},
                                   timeout_s=timeout)
    except ProbeError as exc:
        # Server stderr lands in the message; a child that logs a token
        # must not hand it back through our output.
        return fail(redact_secrets(sanitize(str(exc)), load_secrets()))
    reply = ToolsReply(name, st,
                       tools=result["tools"],
                       server=result["server"],
                       protocol_version=result["protocol_version"])
    if json_view:
        return emit(reply.to_json())

    server = result["server"] or {}
    via = f" ({server.get('name', '')} {server.get('version', '')})".rstrip()
    print(f"{c('bold', name)}{via} — {len(result['tools'])} tool(s)")
    for tool in result["tools"]:
        desc = f"  {tool['description']}" if tool["description"] else ""
        print(f"  {c('cyan', tool['name'])}{desc}")
    return 0


COMMANDS = {
    "discover": cmd_mcp_discover,
    "list": cmd_list,
    "ls": cmd_list,
    "status": cmd_status,
    "sync": cmd_sync,
    "diff": cmd_diff,
    "edit": cmd_edit,
    "validate": cmd_validate,
    "add": cmd_add,
    "remove": cmd_remove,
    "enable": cmd_enable,
    "disable": cmd_disable,
    "set": cmd_set,
    "rename": cmd_rename,
    "set-type": cmd_set_type,
    "info": cmd_info,
    "tools": cmd_tools,
    "secret": cmd_secret,
    "doctor": cmd_doctor,
    "gateway": cmd_gateway,
    "help": cmd_help,
}


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        return cmd_help()
    cmd = args[0]
    if cmd == "help":
        return cmd_help(args[1:])
    if cmd not in COMMANDS:
        print(f"Unknown command: {cmd}")
        print(f"Available: {', '.join(COMMANDS.keys())}")
        return 1
    try:
        return COMMANDS[cmd](args[1:])
    except ConfigurationError as exc:
        # Under --json stdout is one document, so the error goes to stderr.
        print(c("red", str(exc)), file=error_stream("--json" in args))
        return 1


if __name__ == "__main__":
    sys.exit(main())
