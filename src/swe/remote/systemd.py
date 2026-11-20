"""Run the hub as a systemd user service on Linux, so it outlives a reboot.

The Linux twin of ``launchd.py``: ``swe remote serve --install`` writes
``~/.config/systemd/user/swe-remote-hub.service``, enables it and
(re)starts it; ``swe remote agent --install`` does the same for the
satellite under ``swe-remote-agent.service``. Hubs before the
quiver -> swe rename installed ``quiver-remote-*.service``: install and
uninstall stop, disable and remove that unit too (``old_unit``), the
way ``launchd.py`` clears ``OLD_LABEL``. ``Restart=always`` brings the hub back when it exits,
which is also how a ``--tailnet`` hub waits for Tailscale at boot: it
exits until the tailnet address exists.

A user service stops when its user logs out unless lingering is on, so
a home server would lose its hub with the last SSH session. ``install``
turns lingering on when the system lets a user do that for themself
(polkit usually does), and says so when it needs ``sudo``.

Logic only: callers print. ``run`` is injectable so tests never touch
the real systemctl.
"""

from __future__ import annotations

import getpass
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

UNIT = "swe-remote-hub.service"
# The satellite (``swe remote agent --install``) gets its own unit, so
# installing one never rewrites or stops the other.
AGENT_UNIT = "swe-remote-agent.service"
# The units installed before the rename: install clears them so an old
# unit cannot keep a second hub (or satellite) running beside the new
# one, and status still reports one that is all a machine has.
OLD_UNIT = "quiver-remote-hub.service"
OLD_AGENT_UNIT = "quiver-remote-agent.service"
HUB_DESCRIPTION = "SWE remote hub (swe remote serve)"
# Seconds systemd waits before restarting a hub that exited (launchd's
# ThrottleInterval twin).
RESTART_S = 15

Runner = Callable[..., subprocess.CompletedProcess]


def unit_path(home: Path | None = None, unit: str = UNIT) -> Path:
    if home is None and os.environ.get("XDG_CONFIG_HOME"):
        base = Path(os.environ["XDG_CONFIG_HOME"])
    else:
        base = (home or Path.home()) / ".config"
    return base / "systemd" / "user" / unit


def _quote(arg: str) -> str:
    """One ExecStart word: systemd splits on spaces and reads C escapes
    inside double quotes, and ``%`` starts a specifier."""
    arg = arg.replace("%", "%%")
    if arg and not any(ch in arg for ch in ' \t"\\\'$;'):
        return arg
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_unit(program: list[str], log_path: Path, path_env: str,
               description: str = HUB_DESCRIPTION) -> str:
    """The unit: run ``program`` at boot (with lingering) and on exit.

    systemd starts user services with a bare PATH, and the hub shells out
    to tailscale, tmux and the harness CLIs, so the installing shell's
    PATH goes in. Output appends to the same log the Mac uses.
    """
    exec_start = " ".join(_quote(a) for a in program)
    return "\n".join([
        "[Unit]",
        f"Description={description}",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        f"ExecStart={exec_start}",
        "Restart=always",
        f"RestartSec={RESTART_S}",
        f"Environment={_quote('PATH=' + path_env)}",
        "Environment=PYTHONUNBUFFERED=1",
        f"StandardOutput=append:{log_path}",
        f"StandardError=append:{log_path}",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ])


def _ctl(run: Runner, *args: str) -> subprocess.CompletedProcess:
    return run(["systemctl", "--user", *args], capture_output=True, text=True,
               stdin=subprocess.DEVNULL)


def available(run: Runner = subprocess.run) -> bool:
    """Whether this login has a systemd user manager to talk to."""
    try:
        return _ctl(run, "show-environment").returncode == 0
    except OSError:
        return False


def keeper_unit(slug: str) -> str:
    """The transient unit one session keeper runs in."""
    return f"swe-keeper-{slug}"


def keeper_scope_argv(slug: str, run: Runner = subprocess.run,
                      which: Callable[[str], str | None] = shutil.which,
                      platform: str = sys.platform) -> list[str] | None:
    """The command prefix that starts a keeper in its own unit, or None
    where there is no systemd user manager to ask (macOS, a container,
    a login without one).

    The hub's service is stopped with ``KillMode=control-group``, which
    kills every process in the service's cgroup, detached or not. A
    keeper started through ``systemd-run --user --unit=swe-keeper-<slug>
    --collect`` sits in a cgroup of its own and is left alone.
    ``--scope`` makes systemd-run exec the keeper itself, so the keeper
    keeps the hub's environment (the agent's keys never ride on a
    command line) and its pid. ``--collect`` drops the unit even when
    the keeper failed, so the name is free for the next one.
    """
    if not platform.startswith("linux") or which("systemd-run") is None:
        return None
    if not available(run):
        return None
    return ["systemd-run", "--user", "--scope", "--quiet", "--collect",
            f"--unit={keeper_unit(slug)}", "--"]


def _message(res: subprocess.CompletedProcess, what: str) -> str:
    return (res.stderr or res.stdout or "").strip() or f"systemctl {what} exited {res.returncode}"


def _other(unit: str, old_unit: str | None) -> str | None:
    return old_unit if old_unit and old_unit != unit else None


def _names(unit: str, old_unit: str | None) -> tuple[str, ...]:
    """``unit``, then ``old_unit`` when it names a different unit."""
    old = _other(unit, old_unit)
    return (unit, old) if old else (unit,)


def _stopped(run: Runner, unit: str) -> bool:
    return (_ctl(run, "is-active", unit).stdout or "").strip() in {"inactive", "failed"}


def install(program: list[str], log_path: Path, path_env: str,
            home: Path | None = None, run: Runner = subprocess.run,
            unit: str = UNIT, description: str = HUB_DESCRIPTION,
            old_unit: str | None = None) -> tuple[Path, str]:
    """Write the unit, enable it and (re)start it. Returns the unit path
    and a note on lingering ("" when it is on).

    ``old_unit``, the name a pre-rename hub installed under, is stopped,
    disabled and removed once the new unit has started, as launchd's
    ``old_label`` is: left enabled it would start a second copy at
    every boot. Its file goes only once it is really stopped, so a unit
    systemctl could not stop stays where it can be inspected and
    stopped by hand.

    Raises RuntimeError with systemctl's message when a step fails.
    """
    path = unit_path(home, unit)
    path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_unit(program, log_path, path_env, description), encoding="utf-8")
    for args in (("daemon-reload",), ("enable", unit), ("restart", unit)):
        res = _ctl(run, *args)
        if res.returncode != 0:
            raise RuntimeError(_message(res, args[0]))
    old = _other(unit, old_unit)
    if old and unit_path(home, old).exists():
        # A new hub that found the port still held by the old one exits
        # and is restarted RESTART_S later, by when this has freed it.
        _ctl(run, "disable", "--now", old)
        if _stopped(run, old):
            unit_path(home, old).unlink()
            _ctl(run, "daemon-reload")
    return path, ensure_linger(run)


def uninstall(home: Path | None = None, run: Runner = subprocess.run,
              unit: str = UNIT, old_unit: str | None = None) -> bool:
    """Stop and disable the hub and remove the unit. False when none was
    installed.

    ``old_unit`` clears the pre-rename unit too, so an uninstall really
    leaves nothing behind.

    Raises RuntimeError, keeping the unit, when systemctl fails to stop a
    hub that is still active; a failed disable of an absent unit is the
    normal "nothing to stop" case.
    """
    existed = False
    for name in _names(unit, old_unit):
        path = unit_path(home, name)
        if name != unit and not path.exists():
            continue
        here = path.exists()
        res = _ctl(run, "disable", "--now", name)
        if res.returncode != 0 and here and not _stopped(run, name):
            # "active", "activating", or no answer from the manager at
            # all: the hub may still be serving, so keep its unit.
            raise RuntimeError(_message(res, "disable"))
        if here:
            path.unlink()
            _ctl(run, "daemon-reload")
            existed = True
    return existed


def lingering(run: Runner = subprocess.run, user: str | None = None) -> bool | None:
    """Whether this user's services keep running after logout; None when
    loginctl can't say."""
    try:
        res = run(["loginctl", "show-user", user or getpass.getuser(), "--property=Linger", "--value"],
                  capture_output=True, text=True, stdin=subprocess.DEVNULL)
    except OSError:
        return None
    if res.returncode != 0:
        return None
    return (res.stdout or "").strip() == "yes"


LINGER_FIX = "sudo loginctl enable-linger $USER"


def ensure_linger(run: Runner = subprocess.run) -> str:
    """Turn lingering on unless it is known to be on. "" when it's on,
    else what to run (also when loginctl can't say)."""
    if lingering(run):
        return ""
    try:
        run(["loginctl", "enable-linger"], capture_output=True, text=True,
            stdin=subprocess.DEVNULL)
    except OSError:
        pass
    return "" if lingering(run) else LINGER_FIX


def _exec_start(text: str) -> list[str] | None:
    args = None
    for line in text.splitlines():
        if line.startswith("ExecStart="):
            try:
                args = [a.replace("%%", "%") for a in shlex.split(line[len("ExecStart="):])]
            except ValueError:
                args = None
    return args


def status(home: Path | None = None, run: Runner = subprocess.run,
           unit: str = UNIT, old_unit: str | None = None
           ) -> tuple[list[str] | None, bool]:
    """(ExecStart words, active): the installed unit's command line, or
    None without a unit, and whether systemd runs it now.

    A machine the rename never reached reports its ``old_unit``, so
    status does not call a still-running pre-rename hub absent. An
    active unit wins over a stopped one whichever name carries it.
    """
    found = False
    args: list[str] | None = None
    for name in _names(unit, old_unit):
        try:
            text = unit_path(home, name).read_text(encoding="utf-8")
        except OSError:
            continue
        read = _exec_start(text)
        try:
            active = (_ctl(run, "is-active", name).stdout or "").strip() == "active"
        except OSError:
            active = False
        if active:
            return read, True
        if not found:
            found, args = True, read
    return args, False
