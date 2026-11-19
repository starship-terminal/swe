"""``swe mcp set`` and ``swe mcp rename``: change a server in place.

Both start at the hub. ``set`` rewrites fields of the hub entry (URL,
command, env and header keys) and then pushes the new definition to every
harness that already holds the server, through the same sync ``enable``
uses. ``rename`` moves the hub entry, the ``swe`` block's records about it
and the entry in each harness file to a new name, keeping every harness's
own copy as it was.
"""

from __future__ import annotations

import sys
from contextlib import redirect_stdout

from swe.console import c
from swe.flags import expand_value_flags
from swe.json_output import emit, error_stream, take_json_flag
from swe.mcp import gateway as gateway_defaults
from swe.mcp.models import RenameReply, SetReply

SET_USAGE = ("swe mcp set <name> [--url=<url>] [--env=K=V] [--unset-env=K] "
             "[--header=K=V] [--unset-header=K] [-- <command> [args…]]")
RENAME_USAGE = "swe mcp rename <old> <new>"

_VALUE_FLAGS = {"--url", "--env", "--unset-env", "--header", "--unset-header"}


def _holders(cli, name: str) -> list[str]:
    """The harnesses whose config holds ``name``, in swe's display order."""
    mcp_tools = cli.get_mcp_tools(cli.load_registry())
    return [t for t in cli._display_tools(mcp_tools)
            if not cli.is_hub(t) and name in cli.get_tool_servers(t)]


def updated_entry(entry: dict, *, url: str | None, command: list[str] | None,
                  env: dict[str, str], unset_env: list[str],
                  headers: dict[str, str], unset_headers: list[str]) -> dict:
    """``entry`` with the given fields changed; raises ValueError on a mix
    the result cannot hold (env on a URL server, headers on a command one).

    A new URL drops the command side and the other way round, so a server
    can move between the two shapes. swe's own keys (``shared``, a label
    in ``type``) ride along untouched; the ``sse`` marker only survives
    while the entry stays a URL.
    """
    if url is not None and command:
        raise ValueError("Give --url=<url> or `-- <command> [args…]`, not both.")
    out = dict(entry)
    if url is not None:
        out["url"] = url
        for key in ("command", "args", "env"):
            out.pop(key, None)
    elif command:
        out["command"] = command[0]
        if len(command) > 1:
            out["args"] = command[1:]
        else:
            out.pop("args", None)
        for key in ("url", "headers"):
            out.pop(key, None)
        if gateway_defaults.is_transport_marked(entry):
            out.pop(gateway_defaults.TYPE_KEY, None)

    is_url = bool(out.get("url"))
    if (env or unset_env) and is_url:
        raise ValueError("--env only applies to a command server; use --header with a URL.")
    if (headers or unset_headers) and not is_url:
        raise ValueError("--header only applies to a URL server; use --env with a command.")
    for field, sets, unsets in (("env", env, unset_env),
                                ("headers", headers, unset_headers)):
        if not sets and not unsets:
            continue
        current = out.get(field)
        pairs = dict(current) if isinstance(current, dict) else {}
        pairs.update(sets)
        for key in unsets:
            pairs.pop(key, None)
        if pairs:
            out[field] = pairs
        else:
            out.pop(field, None)
    return out


def _drop_kept_copies(cli, name: str) -> None:
    """Forget the raw copies kept for harnesses where ``name`` is off.

    They hold the definition from before this edit, and ``enable`` prefers
    a kept copy over the hub: left in place, switching the server back on
    would restore the old URL or command. The off marks stay, so the
    server remains off there and the next ``enable`` writes the hub's.
    """
    state = cli._swe_state()
    defs_ = state.get("disabled_defs")
    if not isinstance(defs_, dict):
        return
    stale = [h for h, entries in defs_.items()
             if isinstance(entries, dict) and name in entries]
    if not stale:
        return
    for harness in stale:
        del defs_[harness][name]
        if not defs_[harness]:
            del defs_[harness]
    if not defs_:
        state.pop("disabled_defs", None)
    cli._save_swe_state(state)


def cmd_set(args):
    from swe.mcp import cli

    command_tail = None
    if "--" in args:
        cut = args.index("--")
        command_tail, args = args[cut + 1:], args[:cut]
    args, json_view = take_json_flag(list(args))
    if args and args[0] in ("-h", "--help", "help"):
        print(cli.MCP_HELP["set"])
        return 0
    refusal = error_stream(json_view)
    try:
        args = expand_value_flags(args, _VALUE_FLAGS)
    except ValueError as exc:
        print(c("red", str(exc)), file=refusal)
        return 1

    positional: list[str] = []
    picked: dict[str, list[str]] = {flag: [] for flag in _VALUE_FLAGS}
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in _VALUE_FLAGS:
            picked[arg].append(args[i + 1])
            i += 2
        elif arg.startswith("--"):
            print(f"Unknown flag: {arg}", file=refusal)
            return 1
        else:
            positional.append(arg)
            i += 1
    changes = command_tail or any(picked.values())
    if len(positional) != 1 or not changes or len(picked["--url"]) > 1:
        print(f"Usage: {c('cyan', SET_USAGE)}", file=refusal)
        return 1
    name = positional[0]

    hub = cli.get_hub_servers()
    entry = hub.get(name)
    if not isinstance(entry, dict):
        print(f"{name} is not in the hub — `swe mcp sync` adopts a "
              "harness-only server, `swe mcp add` creates one.", file=refusal)
        return 1

    try:
        changed = updated_entry(
            entry,
            url=picked["--url"][0] if picked["--url"] else None,
            command=command_tail,
            env=cli._add_pairs(picked["--env"], "--env"),
            unset_env=picked["--unset-env"],
            headers=cli._add_pairs(picked["--header"], "--header"),
            unset_headers=picked["--unset-header"])
    except ValueError as exc:
        print(str(exc), file=refusal)
        return 1
    changed = cli.redact_secrets(changed)
    if changed == entry:
        if json_view:
            return emit(SetReply(name, changed=False).to_json())
        print(f"{name}: nothing to change")
        return 0

    holders = _holders(cli, name)
    if changed.get("url"):
        # A config that only launches commands cannot hold a URL server:
        # sync would skip it and leave the old command in place.
        local_only = [t for t in holders if not cli.takes_remote(t)]
        if local_only:
            print(c("red", f"{', '.join(local_only)} {cli.LOCAL_ONLY_NOTE}. "
                           f"Switch {name} off there first: swe mcp disable "
                           f"{name} --for={','.join(local_only)}"), file=refusal)
            return 1
    if holders:
        # A ${NAME} no harness can resolve must not land as the literal:
        # refuse before the hub moves, so hub and harnesses never split.
        missing = cli.unresolved_names(changed, cli.load_secrets())
        if missing:
            for var in missing:
                print(f"Not saving: no value for {var} (used by {name}). "
                      f"Set it with: swe mcp secret set {var}", file=refusal)
            return 1

    hub[name] = changed
    cli.save_hub_servers(hub)
    _drop_kept_copies(cli, name)

    synced: list[str] = []
    failed: list[str] = []
    for target in holders:
        # --json's stdout is one document: the inner sync reports to stderr.
        report: dict = {"targets": []}
        with redirect_stdout(sys.stderr if json_view else sys.stdout):
            try:
                code = cli._sync([cli.HUB_LABEL, target, f"--only={name}",
                                  "--no-interactive", "--force"], report)
            except Exception as exc:
                print(c("yellow", f"  {target}: {exc.__class__.__name__}: {exc}"),
                      file=sys.stderr)
                code = 1
        # A sync can exit 0 having left the server out. A harness whose
        # copy needed no change (a shared server's gateway URL) is fine;
        # one the run skipped, could not express or holds off is not.
        left_out = any(
            row.get("name") == target and name in (
                (row.get("skipped") or []) + (row.get("unsupported") or [])
                + (row.get("disabled") or []))
            for row in report.get("targets") or [])
        (failed if code != 0 or left_out else synced).append(target)

    if json_view:
        emit(SetReply(name, changed=True, synced=synced, failed=failed).to_json())
    else:
        print(f"{c('green', '✓')} {name} updated in the SWE hub")
        for target in failed:
            print(f"  {target}: still holds the old definition", file=refusal)
    return 1 if failed else 0


def _renamed_keys(mapping: dict, old: str, new: str) -> dict:
    """``mapping`` with key ``old`` called ``new``, order kept."""
    return {(new if key == old else key): value for key, value in mapping.items()}


def _repoint_gateway(value, old_url_tail: str, new_url_tail: str):
    """A harness entry with its gateway URL moved to the new name.

    A shared server's harness copy is the loopback URL ``…/mcp/<name>``,
    in whatever key that format uses, so the string is found by value.
    """
    if isinstance(value, dict):
        return {k: _repoint_gateway(v, old_url_tail, new_url_tail)
                for k, v in value.items()}
    if (isinstance(value, str) and value.endswith(old_url_tail)
            and value.startswith(f"http://{gateway_defaults.HOST}:")):
        return value[:-len(old_url_tail)] + new_url_tail
    return value


def rename_state(state: dict, old: str, new: str) -> bool:
    """Move every record about ``old`` in mcp.json's ``swe`` block to
    ``new``; True when anything changed. The caller saves ``state``."""
    changed = False
    disabled = state.get("disabled")
    if isinstance(disabled, dict):
        for harness, names in disabled.items():
            if isinstance(names, list) and old in names:
                disabled[harness] = [new if n == old else n for n in names]
                changed = True
    defs_ = state.get("disabled_defs")
    if isinstance(defs_, dict):
        for harness, entries in defs_.items():
            if isinstance(entries, dict) and old in entries:
                defs_[harness] = _renamed_keys(entries, old, new)
                changed = True
    for key in ("master_off", "adopted_from"):
        record = state.get(key)
        if isinstance(record, dict) and old in record:
            state[key] = _renamed_keys(record, old, new)
            changed = True
    return changed


def cmd_rename(args):
    from swe.mcp import cli

    args, json_view = take_json_flag(list(args))
    if args and args[0] in ("-h", "--help", "help"):
        print(cli.MCP_HELP["rename"])
        return 0
    refusal = error_stream(json_view)
    for arg in args:
        if arg.startswith("--"):
            print(f"Unknown flag: {arg}", file=refusal)
            return 1
    if len(args) != 2:
        print(f"Usage: {c('cyan', RENAME_USAGE)}", file=refusal)
        return 1
    old, new = args
    if not cli._SERVER_NAME_RE.match(new):
        print("A name is letters, digits, . _ @ - and can't start with _ . @ -.",
              file=refusal)
        return 1
    if old == new:
        print(f"{old} already has that name.", file=refusal)
        return 1

    mcp_tools = cli.get_mcp_tools(cli.load_registry())
    harnesses = [t for t in cli._display_tools(mcp_tools) if not cli.is_hub(t)]
    held = {t: cli.get_tool_servers(t) for t in harnesses}
    hub = cli.get_hub_servers()
    holders = [t for t in harnesses if old in held[t]]
    if old not in hub and not holders:
        print(f"{old} is not in the hub or any harness.", file=refusal)
        return 1
    taken = ([cli.HUB_LABEL] if new in hub else []) + [t for t in harnesses if new in held[t]]
    if taken:
        print(c("red", f"{new} already exists in: {', '.join(taken)}."), file=refusal)
        return 1
    # Checked before anything moves: a harness swe cannot write would be
    # left holding the old name while the hub already forgot it.
    unwritable = [t for t in holders if cli.get_tool_saver(t) is None]
    if unwritable:
        print(c("red", f"Cannot write the config of: {', '.join(unwritable)}."),
              file=refusal)
        return 1

    in_hub = old in hub
    old_tail = gateway_defaults.PATH_PREFIX + old
    new_tail = gateway_defaults.PATH_PREFIX + new

    # Harness files first: a config that looks writable can still refuse
    # the write (a read-only directory), and the hub must not forget the
    # old name while a harness still holds it. A failure puts back the
    # files already rewritten and leaves the hub and its records alone.
    renamed: list[str] = []
    failed: list[str] = []
    for target in holders:
        try:
            servers = _renamed_keys(held[target], old, new)
            if in_hub and gateway_defaults.is_gateway_entry(
                    old, cli.get_mcp_handler(target).parse(servers[new])):
                servers[new] = _repoint_gateway(servers[new], old_tail, new_tail)
            cli.get_tool_saver(target)(servers, cli.get_tool_config(target)["path"])
            renamed.append(target)
        except Exception as exc:
            failed.append(target)
            print(c("yellow", f"  {target}: could not rename {old} "
                            f"({exc.__class__.__name__}: {exc})"), file=sys.stderr)
            break
    if failed:
        for target in renamed:
            try:
                cli.get_tool_saver(target)(held[target], cli.get_tool_config(target)["path"])
            except Exception as exc:
                failed.append(target)
                print(c("yellow", f"  {target}: could not put {old} back "
                                f"({exc.__class__.__name__}: {exc})"), file=sys.stderr)
        if json_view:
            emit(RenameReply(old, old, hub=False, failed_in=failed).to_json())
        else:
            print(c("red", f"{old} keeps its name: could not write "
                           f"{', '.join(failed)}."), file=refusal)
        return 1

    if in_hub:
        cli.save_hub_servers(_renamed_keys(hub, old, new))
    state = cli._swe_state()
    if rename_state(state, old, new):
        if in_hub:
            # A kept copy of a shared server is its gateway URL: it must
            # follow the name too, or `enable` restores /mcp/<old>.
            for harness, entries in (state.get("disabled_defs") or {}).items():
                if isinstance(entries, dict) and isinstance(entries.get(new), dict):
                    entries[new] = _repoint_gateway(entries[new], old_tail, new_tail)
        cli._save_swe_state(state)

    if json_view:
        emit(RenameReply(new, old, hub=in_hub, renamed_in=renamed,
                         failed_in=failed).to_json())
    else:
        places = ([cli.HUB_LABEL] if in_hub else []) + renamed
        print(f"{c('green', '✓')} {old} is now {c('cyan', new)} in {', '.join(places)}")
    return 1 if failed else 0
