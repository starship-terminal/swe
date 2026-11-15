#!/usr/bin/env python3
"""swe — Central manager for AI coding CLI tools."""

import sys

from importlib import import_module

from swe import __version__
from swe.json_output import emit, error_stream, take_json_flag
from swe.console import c


def _configuration_error() -> type[Exception]:
    """``ConfigurationError``, imported only once something was raised:
    an ``except`` clause evaluates its class when an exception reaches it,
    and ``swe.configuration`` pulls ``subprocess`` and ``tempfile`` in,
    which `swe version` never needs."""
    from swe.configuration import ConfigurationError
    return ConfigurationError


def _lazy(module: str, name: str):
    """A command that imports ``module`` only when it runs.

    Importing every command module up front cost most of a `swe version`
    run (#1536): one command needs one module, and the app spawns `swe`
    many times an hour. Tests that introspect ``COMMANDS`` see the same
    keys; patch the real module's function, not this shim.
    """
    def command(args):
        return getattr(import_module(module), name)(args)

    command.__name__ = command.__qualname__ = name
    command.__doc__ = f"``{module}.{name}``, imported when run."
    return command


cmd_add = _lazy("swe.harness.commands", "cmd_add")
cmd_aliases = _lazy("swe.harness.commands", "cmd_aliases")
cmd_check = _lazy("swe.harness.commands", "cmd_check")
cmd_doctor = _lazy("swe.harness.commands", "cmd_doctor")
cmd_edit = _lazy("swe.harness.commands", "cmd_edit")
cmd_info = _lazy("swe.harness.commands", "cmd_info")
cmd_install = _lazy("swe.harness.commands", "cmd_install")
cmd_list = _lazy("swe.harness.commands", "cmd_list")
cmd_remove = _lazy("swe.harness.commands", "cmd_remove")
cmd_tags = _lazy("swe.harness.commands", "cmd_tags")
cmd_use = _lazy("swe.harness.commands", "cmd_use")
cmd_bug = _lazy("swe.bug", "cmd_bug")
cmd_config = _lazy("swe.config_commands", "cmd_config")
cmd_find = _lazy("swe.find", "cmd_find")
cmd_help = _lazy("swe.help_text", "cmd_help")
cmd_hooks = _lazy("swe.hooks", "cmd_hooks")
cmd_init = _lazy("swe.init", "cmd_init")
cmd_instructions = _lazy("swe.instructions.commands", "cmd_instructions")
cmd_gh = _lazy("swe.github.cli", "main")
cmd_mcp = _lazy("swe.mcp", "main")
cmd_plugins = _lazy("swe.plugins", "cmd_plugins")
cmd_providers = _lazy("swe.providers.cli", "main")
cmd_report = _lazy("swe.reports.commands", "cmd_report")
cmd_models = _lazy("swe.sessions.commands", "cmd_models")
cmd_session = _lazy("swe.sessions.commands", "cmd_session")
cmd_harness = _lazy("swe.setup.commands", "cmd_harness")
cmd_setup = _lazy("swe.setup.commands", "cmd_setup")
cmd_skills = _lazy("swe.skills.commands", "cmd_skills")
cmd_sync = _lazy("swe.sync", "cmd_sync")
cmd_worktree = _lazy("swe.worktrees.commands", "cmd_worktree")
# swe.remote pulls sessions, reports and the hub in (#517).
cmd_remote = _lazy("swe.remote", "cmd_remote")


_HELP_NAME = {"-v": "version", "-V": "version", "--version": "version"}


def cmd_version(args):
    """swe version / -v / -V / --version. Reads no config, so it works even
    when ~/.swe is unparseable."""
    args, json_view = take_json_flag(args)
    if args:
        print(c("red", f"Unexpected args: {' '.join(args)}"),
              file=error_stream(json_view))
        return 1
    if json_view:
        import platform

        return emit({"name": "swe", "version": __version__,
                     "python": platform.python_version()})
    print(f"swe {__version__}")
    return 0


def cmd_complete(args):
    """Hidden command for shell completion. Outputs candidates to stdout."""
    from swe.completion import get_completions
    from swe.console import sanitize

    completions = get_completions(args)
    for candidate, desc in completions:
        # One candidate per line is the whole protocol — a server name or
        # description carrying \n or \t would inject extra lines into it.
        print(f"{sanitize(candidate)}\t{sanitize(desc)}")
    # Directive: 4 = NoFileComp (suppress file completion fallback)
    print(":4")
    return 0


def cmd_autocomplete(args):
    """Generate and inject shell completion script (or print it with --print)."""
    from swe.completion_scripts import SHELL_CONFIGS
    from swe.console import c
    from swe.paths import COMPLETION_DIR

    # --print is a boolean: check it on raw argv, wherever it sits.
    print_only = "--print" in args
    args = [a for a in args if a != "--print"]

    supported = list(SHELL_CONFIGS.keys())
    if not args or args[0] not in supported:
        print(f"Usage: swe autocomplete [{'|'.join(supported)}] [--print]")
        if args and args[0] not in supported:
            print(c("red", f"Unsupported shell: '{args[0]}'"))
        return 1
    if len(args) > 1:
        print(c("red", f"Unexpected args: {' '.join(args[1:])}"))
        return 1

    shell = args[0]
    config = SHELL_CONFIGS[shell]
    import os

    if print_only:
        # Preview: the script only, nothing on disk, no rc edit.
        sys.stdout.write(config["script"])
        return 0

    # 1. Write completion script to ~/.swe/completions/
    script_dir = os.path.expanduser(str(COMPLETION_DIR))
    os.makedirs(script_dir, exist_ok=True)
    script_path = os.path.join(script_dir, config["filename"])
    try:
        with open(script_path, "w") as f:
            f.write(config["script"])
    except Exception as e:
        print(c("red", f"Failed to write completion script: {e}"))
        return 1

    # 2. Inject source line into shell profile (idempotent)
    profile_path = os.path.expanduser(config["profile"])
    source_line = f'source "{script_path}"  # swe autocomplete ({shell})'

    already_present = False
    try:
        if os.path.exists(profile_path):
            with open(profile_path) as f:
                content = f.read()
            if script_path in content or f"swe autocomplete ({shell})" in content:
                already_present = True
    except Exception:
        pass

    if not already_present:
        try:
            # Create profile if it doesn't exist
            os.makedirs(os.path.dirname(profile_path), exist_ok=True)
            with open(profile_path, "a") as f:
                f.write(f"\n{source_line}\n")
        except Exception as e:
            print(c("red", f"Failed to update {config['profile']}: {e}"))
            print(f"  Completion script written to: {script_path}")
            print(f"  Manually add this line to {config['profile']}:")
            print(f"    {source_line}")
            return 1

    # 3. Say what was written and edited
    print(f"Wrote {script_path}.")
    if already_present:
        print(f"{profile_path} already has the source line, so I left it alone.")
    else:
        print(f"Added a source line to {profile_path}.")
    print(f"Source your {shell} profile to apply the changes or restart your terminal.")
    print(f"For manual sourcing, use: {config['profile_instructions']}")
    return 0


COMMANDS = {
    # Shortcuts for `swe harness list`, which is where the harness verbs
    # live. Kept at the top level because it is the command run most.
    "list": cmd_list,
    "ls": cmd_list,
    "info": cmd_info,
    "add": cmd_add,
    "edit": cmd_edit,
    "remove": cmd_remove,
    "rm": cmd_remove,
    "use": cmd_use,
    "run": cmd_use,
    "check": cmd_check,
    "doctor": cmd_doctor,
    "install": cmd_install,
    "session": cmd_session,
    "models": cmd_models,
    "skills": cmd_skills,
    "sk": cmd_skills,
    "tags": cmd_tags,
    "aliases": cmd_aliases,
    "mcp": cmd_mcp,
    "gh": cmd_gh,
    "harness": cmd_harness,
    "hooks": cmd_hooks,
    "hs": cmd_harness,
    "setup": cmd_setup,
    "init": cmd_init,
    "instructions": cmd_instructions,
    "find": cmd_find,
    "plugins": cmd_plugins,
    "providers": cmd_providers,
    "pv": cmd_providers,
    "config": cmd_config,
    "remote": cmd_remote,
    "sync": cmd_sync,
    "worktree": cmd_worktree,
    "wt": cmd_worktree,
    "report": cmd_report,
    "discover": lambda args: cmd_harness(["discover", *args]),
    "autocomplete": cmd_autocomplete,
    "bug": cmd_bug,
    "version": cmd_version,
    "-v": cmd_version,
    "-V": cmd_version,
    "--version": cmd_version,
    "help": cmd_help,
    "--help": cmd_help,
    "-h": cmd_help,
    # Hidden completion command (not shown in help)
    "__complete": cmd_complete,
}


def main():
    argv = sys.argv[1:]
    if not argv:
        try:
            cmd_help([])
        except _configuration_error() as exc:
            print(c("red", str(exc)))
            return 1
        return 0
    cmd = argv[0]
    rest = argv[1:]
    if cmd in COMMANDS:
        try:
            if rest and rest[0] in ("--help", "-h"):
                # providers carries its own fuller help text; routing
                # through cmd_help would print the summary that ends by
                # pointing back at `swe providers --help` itself.
                if cmd in ("providers", "pv"):
                    return COMMANDS[cmd](rest)
                # The version flags are spellings of `version`; the help
                # topic lives under the canonical name.
                cmd_help([_HELP_NAME.get(cmd, cmd)])
                return 0
            result = COMMANDS[cmd](rest)
        except _configuration_error() as exc:
            # Under --json the app decodes stdout whole, so the message goes
            # to stderr there; everywhere else it stays where it always was.
            print(c("red", str(exc)), file=error_stream("--json" in rest))
            return 1
        return result if isinstance(result, int) else 0
    print(f"Unknown command: '{cmd}'")
    try:
        cmd_help([])
    except _configuration_error() as exc:
        print(c("red", str(exc)))
    return 1


if __name__ == "__main__":
    sys.exit(main())
