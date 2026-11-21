"""``swe remote serve|pair|status|devices|attach|ssh`` — argv + print only.

The serve command runs the hub in the foreground; ``serve --install``
hands it to a LaunchAgent that restarts it after a crash or a reboot
(``launchd.py``), and a systemd unit does the same on Linux. ``pair`` mints a one-time token into
``pair_tokens.json`` that the running hub honors on ``POST /v1/pair``;
``status`` probes the hub and reports reachability, machines and paired
devices.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from swe import __version__
from swe.console import c
from swe.flags import expand_value_flags
from swe.json_output import emit, error_stream, take_json_flag
from swe.paths import remote_dir_for
from swe.remote import agent as agent_mod
from swe.remote import catalog as catalog_mod
from swe.remote import devices as devices_mod
from swe.remote import (
    launchd, notify, pairing, protocol, sshkeys, supervise, tail,
)
from swe.remote import setup as setup_mod
from swe.remote import systemd
from swe.remote.hub import (
    DEFAULT_PORT,
    WILDCARD_HOSTS,
    HubState,
    check_public_url,
    load_hub_config,
    serve,
)

_SUBCOMMANDS = {"serve", "pair", "status", "devices", "attach", "agent",
                "notify", "setup", "ssh"}

_VALUE_FLAGS = {
    "--host", "--port", "--name", "--tail-days", "--revoke", "--mode",
    "--hub", "--token", "--ntfy", "--topic", "--ntfy-token", "--service-status",
    "--approve", "--deny", "--request-id", "--public-url", "--model",
    "--effort", "--auto-approve",
}
_BOOL_FLAGS = {"--on", "--off", "--app", "--requests", "--install",
               "--uninstall", "--tailnet", "--apply", "--recheck",
               "--unsafe-bind-any", "--status"}

_USAGE = f"""\
  {c('cyan', 'swe remote serve [--host=H] [--port=N]')}   Run the hub (REST + SSE)
  {c('cyan', 'swe remote serve --public-url=URL')}      Run behind tailscale serve (binds 127.0.0.1)
  {c('cyan', 'swe remote serve --install | --uninstall')}
                                  Keep the hub running (macOS LaunchAgent, Linux systemd unit)
  {c('cyan', 'swe remote setup [--apply] [--recheck] [--json]')}
                                  Check every step to a paired phone; --apply does SWE's part
  {c('cyan', 'swe remote agent --hub=URL [--token=T]')}   Satellite: dial out to the hub
  {c('cyan', 'swe remote agent --install | --uninstall | --status [--json]')}
                                  Keep the satellite running (macOS LaunchAgent, Linux systemd unit)
  {c('cyan', 'swe remote pair [--name=N] [--host=H] [--port=N] [--app] [--json]')}
                                  Print a pairing URL + token for a phone
  {c('cyan', 'swe remote status [--host=H] [--port=N]')}  Probe the hub + list machines
  {c('cyan', 'swe remote devices [--revoke=<id>] [--requests] [--approve=<code>] [--deny=<code>] [--request-id=<id>]')}
                                  List/revoke devices and approve pair requests
  {c('cyan', 'swe remote devices --auto-approve=on|off')}
                                  Pair your own untagged tailnet devices without a code
  {c('cyan', 'swe remote attach <session> [--mode=auto|tmux|headless|serve|appserver|acp] [--model=<m>] [--effort=<e>]')}
                                  Move an observed session under supervision
  {c('cyan', 'swe remote notify [--ntfy=URL --topic=T] [--on|--off] [--service-status=on|off]')}
                                  Configure ntfy push notifications
  {c('cyan', 'swe remote ssh [--on|--off] [--json]')}     Let paired phones' SSH keys log in here
"""


_tailscale_ip = catalog_mod.tailscale_ip


def _parse_flags(args):
    """Returns (subcommand, flags dict, positionals) or raises ValueError."""
    args, json_view = take_json_flag(args)
    try:
        args = expand_value_flags(args, _VALUE_FLAGS)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    flags: dict[str, str] = {}
    positionals: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in _VALUE_FLAGS:
            if i + 1 >= len(args):
                raise ValueError(f"{arg} takes a value: write it as {arg}=<value>")
            flags[arg] = args[i + 1]
            i += 2
        elif arg in _BOOL_FLAGS:
            flags[arg] = "1"
            i += 1
        elif arg.startswith("-") and arg != "-":
            raise ValueError(f"unknown flag: {arg}")
        else:
            positionals.append(arg)
            i += 1
    sub = positionals[0] if positionals else ""
    return sub, flags, positionals[1:], json_view


def _host_port(flags: dict) -> tuple[str, int]:
    host = flags.get("--host") or ""
    try:
        port = int(flags.get("--port") or DEFAULT_PORT)
    except ValueError:
        raise ValueError("--port must be a number")
    return host, port


# The hub is tailnet-only: a loopback bind for local use, or the node's
# own Tailscale address (100.64.0.0/10, or fd7a:115c:a1e0::/48 for v6)
# for devices. Anything else — a wildcard, a LAN address, a public name —
# puts session control on a network it was never meant to face, so
# ``--host`` values outside these ranges need the explicit override.
_TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
_TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")


def _bind_ip_ok(ip) -> bool:
    """True for a loopback or Tailscale address ``serve`` may bind."""
    mapped = getattr(ip, "ipv4_mapped", None)
    return (
        ip.is_loopback
        or (mapped is not None and _bind_ip_ok(mapped))
        or ip in _TAILNET_V4
        or ip in _TAILNET_V6
    )


def _bindable(host: str, port: int) -> str | None:
    """Why ``host`` is refused as the hub's bind address, else None.

    A name is checked against the addresses it resolves to, so
    ``mbp.tail0000.ts.net`` passes like the tailnet IP it names.
    """
    try:
        addrs = [ipaddress.ip_address(host)]
    except ValueError:
        if host == "localhost":
            return None
        try:
            infos = socket.getaddrinfo(
                host, port, type=socket.SOCK_STREAM
            )
        except OSError:
            infos = []
        addrs = []
        for info in infos:
            try:
                addrs.append(ipaddress.ip_address(info[4][0]))
            except ValueError:
                pass
        if not addrs:
            return (f"--host={host} doesn't resolve to an address the hub "
                    f"can bind; pass --unsafe-bind-any to try it anyway")
    if any(a.is_unspecified for a in addrs):
        return (f"--host={host} binds every interface on this machine; "
                "pass --unsafe-bind-any if you really mean that")
    if all(_bind_ip_ok(a) for a in addrs):
        return None
    return (f"--host={host} isn't a loopback or Tailscale address; "
            "pass --unsafe-bind-any to bind it anyway")


def _probe(host: str, port: int, timeout: float = 3.0) -> dict | None:
    url = f"http://{protocol.url_host(host)}:{port}/v1/status"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _saved_hub(flags: dict) -> dict:
    """The last ``serve``'s address, when no --host/--port overrides it."""
    if flags.get("--host") or flags.get("--port"):
        return {}
    return load_hub_config()


def _phone_address(flags: dict) -> tuple[str, int, str]:
    """``(host, port, scheme)`` a phone should dial: the hub's public URL
    when it runs behind ``tailscale serve``, else the flags or the
    tailnet IP."""
    public = str(_saved_hub(flags).get("public_url") or "")
    if public:
        parts = urlsplit(public)
        default = 443 if parts.scheme == "https" else 80
        return parts.hostname or "", parts.port or default, parts.scheme
    host, port = _host_port(flags)
    return host or _tailscale_ip() or "127.0.0.1", port, "http"


def _public_url(flags: dict) -> str:
    """``--public-url`` normalised, or ""; raises ValueError when bad."""
    raw = flags.get("--public-url")
    return check_public_url(raw) if raw else ""


# How long --install waits for the new hub to answer: start-up plus a
# slow `tailscale ip` lookup and one launchd retry.
_INSTALL_WAIT_S = 20


def _stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _serve_bind(flags: dict) -> tuple[str, int, str]:
    """``(host, port, public_url)`` for either serve path, with the
    ``--host`` value held to loopback-or-tailnet unless the caller
    passed ``--unsafe-bind-any``. Raises ValueError like ``_host_port``."""
    host, port = _host_port(flags)
    public_url = _public_url(flags)
    if host and not flags.get("--unsafe-bind-any"):
        bad = _bindable(host, port)
        if bad:
            raise ValueError(bad)
    return host, port, public_url


def _cmd_serve(flags: dict) -> int:
    if flags.get("--install") or flags.get("--uninstall"):
        return _cmd_serve_launchd(flags)
    try:
        host, port, public_url = _serve_bind(flags)
    except ValueError as exc:
        print(c("red", str(exc)), file=sys.stderr)
        return 1
    # Behind tailscale serve the hub listens on loopback only; serve is
    # the one door in.
    if public_url and not host:
        host = "127.0.0.1"
    if not host:
        host = _tailscale_ip() or "127.0.0.1"
        if host == "127.0.0.1" and flags.get("--tailnet"):
            # Under launchd at login Tailscale may not be up yet: exit so
            # launchd retries, rather than serve a hub the phone can't reach.
            print(f"{_stamp()}  no Tailscale address yet; exiting so launchd retries",
                  file=sys.stderr, flush=True)
            return 1
        if host == "127.0.0.1":
            print(
                c("yellow",
                  "no Tailscale address found; binding localhost only"),
                file=sys.stderr,
            )
    note = upgrade_service(HUB)
    if note:
        print(f"{_stamp()}  {note}", flush=True)
    state = HubState()
    # Timestamped and flushed: the log is often a file, and a hub killed
    # by a restart otherwise leaves no trace of when it ran.
    print(
        f"{_stamp()}  remote hub on {c('cyan', f'http://{protocol.url_host(host)}:{port}')} "
        f"as {c('bold', state.machine['name'])} ({state.machine_id})",
        flush=True,
    )
    if public_url:
        print(f"phones reach it at {c('cyan', public_url)}", flush=True)
    print(c("dim", f"state: {remote_dir_for()}"), flush=True)
    # This machine's loopback API (G15): the same session routes for a
    # local client that holds the token in local.json.
    local_api = agent_mod.serve_local(state)
    try:
        rc = serve(state, host, port, public_url)
    except OSError as exc:
        print(c("red", f"{_stamp()}  cannot bind {host}:{port} — {exc}"),
              file=sys.stderr, flush=True)
        return 1
    finally:
        if local_api is not None:
            local_api.close()
    print(f"{_stamp()}  remote hub stopped", flush=True)
    return rc


@dataclass(frozen=True)
class Service:
    """One background service ``--install`` manages: the hub or the
    satellite. Each has its own launchd label and systemd unit, so
    installing one never reloads or stops the other."""

    name: str         # "hub" or "agent", for messages
    command: str      # the ``swe remote`` subcommand it runs
    label: str        # launchd
    unit: str         # systemd
    description: str  # systemd Description=
    log_name: str     # under ~/.swe/data/remote/
    old_label: str    # launchd label the pre-rename app installed under
    old_unit: str     # systemd unit a pre-rename hub installed under


HUB = Service("hub", "serve", launchd.LABEL, systemd.UNIT,
              systemd.HUB_DESCRIPTION, "serve.log", launchd.OLD_LABEL,
              systemd.OLD_UNIT)
AGENT = Service("agent", "agent", launchd.AGENT_LABEL, systemd.AGENT_UNIT,
                "SWE remote agent (swe remote agent)", "agent.log",
                launchd.OLD_AGENT_LABEL, systemd.OLD_AGENT_UNIT)


def _service_swe(service: Service) -> tuple[str, str | None]:
    """The ``swe`` a background service runs, or an error saying why it
    can't run.

    On a Mac it runs SWE.app's bundled swe when the app is installed,
    else this swe, unless that sits where macOS keeps launchd agents out.
    """
    darwin = sys.platform == "darwin"
    bundled = launchd.bundled_swe() if darwin else None
    swe = str(bundled) if bundled else os.path.abspath(sys.argv[0])
    if not bundled:
        if not os.access(swe, os.X_OK):
            return "", (f"cannot find the swe executable ({sys.argv[0]}); "
                        f"run this as `swe remote {service.command} --install`")
        folder = launchd.protected_dir(swe) if darwin else None
        if folder:
            return "", (f"{swe} is in ~/{folder}, which macOS blocks a LaunchAgent "
                        f"from reading, so the {service.name} would fail at every start. "
                        "Install SWE.app (app/mac/script/install-app.sh) and run "
                        "--install again: the agent then runs the app's bundled swe.")
    return swe, None


def hub_program(host: str, port: int | None, public_url: str,
                unsafe_any: bool = False) -> tuple[list[str], str | None]:
    """The hub service's command line, or an error saying why it can't
    run.

    ``port`` is left off the command line when None (the default port);
    ``unsafe_any`` keeps a deliberate off-tailnet bind working once the
    program runs.
    """
    swe, err = _service_swe(HUB)
    if err:
        return [], err
    program = [swe, "remote", "serve"]
    program += [f"--host={host}"] if host else ["--tailnet"]
    if port is not None and port != DEFAULT_PORT:
        program.append(f"--port={port}")
    if public_url:
        program.append(f"--public-url={public_url}")
    if unsafe_any:
        program.append("--unsafe-bind-any")
    return program, None


def _service_problem(service: Service = HUB) -> str | None:
    """Why ``--install`` can't keep ``service`` running here, or None."""
    if sys.platform == "darwin":
        return None
    if sys.platform.startswith("linux"):
        if systemd.available():
            return None
        return (f"no systemd user manager here; run `swe remote {service.command}` "
                "under this machine's init system instead")
    return (f"--install keeps the {service.name} running on macOS (launchd) "
            "and Linux (systemd) only")


def service_file(service: Service = HUB, home: Path | None = None) -> Path:
    """Where ``service``'s LaunchAgent plist or systemd unit lives."""
    if sys.platform == "darwin":
        return launchd.plist_path(home, service.label)
    return systemd.unit_path(home, service.unit)


def service_files(service: Service, home: Path | None = None) -> list[Path]:
    """Every path an install of ``service`` can live at, newest first.

    A pre-rename install left the old label's plist (or the old unit),
    and it keeps a job running exactly like the new one's: conflict and
    presence checks that only look at ``service_file`` would call it
    absent.
    """
    files = [service_file(service, home)]
    if sys.platform == "darwin":
        files.append(launchd.plist_path(home, service.old_label))
    else:
        files.append(systemd.unit_path(home, service.old_unit))
    return files


def service_status(service: Service = HUB, home: Path | None = None,
                   run=subprocess.run) -> tuple[list[str] | None, bool]:
    """(installed command line or None, running) for ``service``."""
    if sys.platform == "darwin":
        return launchd.status(home, run=run, label=service.label,
                              old_label=service.old_label)
    return systemd.status(home, run=run, unit=service.unit,
                          old_unit=service.old_unit)


def service_conflict(service: Service, home: Path | None = None) -> str | None:
    """Why ``service`` must not be installed here, or None: the other
    remote service (hub for the agent, agent for the hub) is installed.

    Both run as this user, so they read the same ~/.swe and share the
    tail cursors under ``data/remote/cursors/``: each would skip
    transcript lines the other already sent, to a different hub, and a
    satellite would send its own hub's sessions back to it.
    """
    other = AGENT if service is HUB else HUB
    path = next((p for p in service_files(other, home) if p.exists()), None)
    if path is None:
        return None
    if service is AGENT:
        what = "this machine's hub is installed"
        fix = ("Run the agent on another machine, or remove the hub first with "
               "`swe remote serve --uninstall`.")
    else:
        what = "this machine's satellite agent is installed"
        fix = ("Host the hub on another machine, or remove the agent first with "
               "`swe remote agent --uninstall`.")
    return (f"{what} ({path}) and keeps its state in the same {remote_dir_for(home)}, "
            f"so the two would share its transcript cursors and each skip what the "
            f"other already sent. {fix}")


def install_service(program: list[str], log_path: Path, home: Path | None = None,
                    run=subprocess.run, service: Service = HUB) -> tuple[Path, str]:
    """Install and (re)start ``service`` as this platform's background
    service.

    Returns its file and a note to print ("" when none); raises
    RuntimeError with the service manager's message, or when the other
    remote service is installed (``service_conflict``), so every install
    path (``serve --install``, ``agent --install``, ``setup --apply``)
    refuses both orders.
    """
    conflict = service_conflict(service, home)
    if conflict:
        raise RuntimeError(conflict)
    path_env = os.environ.get("PATH", "")
    if sys.platform == "darwin":
        try:
            return launchd.install(program, log_path, path_env, home=home, run=run,
                                   label=service.label,
                                   old_label=service.old_label), ""
        except RuntimeError as exc:
            raise RuntimeError(f"launchctl could not load the {service.name}: {exc}") from exc
    try:
        path, linger = systemd.install(program, log_path, path_env, home=home, run=run,
                                       unit=service.unit, description=service.description,
                                       old_unit=service.old_unit)
    except RuntimeError as exc:
        raise RuntimeError(f"systemctl could not start the {service.name}: {exc}") from exc
    notes = []
    if linger:
        notes.append(f"it runs only while you're logged in; to keep it after logout, "
                     f"run `{linger}`")
    old = systemd.unit_path(home, service.old_unit)
    if old.exists():
        # systemd.install keeps the pre-rename unit's file only when it
        # would not stop: that one may still hold the port the new
        # service needs, so the upgrade is not done.
        notes.append(f"the pre-rename {service.old_unit} would not stop and may still be "
                     f"running in place of the new {service.name}; stop it with "
                     f"`systemctl --user disable --now {service.old_unit}`, then run "
                     f"--install again")
    return path, "; ".join(notes)


def upgrade_service(service: Service = HUB, home: Path | None = None,
                    environ=os.environ, popen=subprocess.Popen) -> str:
    """Bring an installed service's file up to this swe, at start, so
    an upgrade needs no ``--install``. Returns a line for the log, or
    "" when nothing changed.

    macOS: an agent installed before session keepers existed lacks
    ``AbandonProcessGroup``. The plist is rewritten in place, and when
    this process is that agent's job it is loaded again from the new
    file (launchd reads a plist only at load). That reload ends this
    process; launchd starts the hub again at once.

    Linux: nothing in the unit file changed for keepers. A keeper is
    put in a scope unit of its own when it is started
    (``systemd.keeper_scope_argv``), so the new code is enough.
    """
    if sys.platform != "darwin":
        return ""
    try:
        if not launchd.upgrade_plist(home, service.label):
            return ""
        path = launchd.plist_path(home, service.label)
        if not launchd.running_as(service.label, environ):
            return (f"updated {path} (AbandonProcessGroup); it takes effect "
                    f"when the {service.name} service next loads")
        launchd.reload_detached(home, service.label, popen=popen)
    except OSError as exc:
        return f"could not update the {service.name} LaunchAgent: {exc}"
    return (f"updated {path} (AbandonProcessGroup) and reloading the "
            f"{service.name} service so it takes effect")


def _cmd_uninstall_service(service: Service) -> int:
    darwin = sys.platform == "darwin"
    try:
        removed = (launchd.uninstall(label=service.label,
                                     old_label=service.old_label) if darwin
                   else systemd.uninstall(unit=service.unit,
                                          old_unit=service.old_unit))
    except RuntimeError as exc:
        print(c("red", f"could not stop the {service.name}: {exc}"), file=sys.stderr)
        return 1
    if removed:
        print(f"stopped the {service.name} and removed {service_file(service)}")
    else:
        print(c("dim", f"no {service.name} {'LaunchAgent' if darwin else 'systemd unit'} "
                       "was installed"))
    return 0


def _print_installed(path: Path, program: list[str], log_path: Path, note: str) -> None:
    darwin = sys.platform == "darwin"
    print(f"installed {path}")
    print(f"  runs  {' '.join(program)}")
    print(f"  log   {log_path}")
    print(c("dim", f"  starts at {'login' if darwin else 'boot'} and restarts if it exits; "
                   "run --install again to restart it, --uninstall to stop it"))
    if note:
        print(c("yellow", f"  {note}"), file=sys.stderr)


def _cmd_serve_launchd(flags: dict) -> int:
    problem = _service_problem(HUB)
    if problem:
        print(c("red", problem), file=sys.stderr)
        return 1
    darwin = sys.platform == "darwin"
    if flags.get("--uninstall"):
        return _cmd_uninstall_service(HUB)
    try:
        host, port, public_url = _serve_bind(flags)
    except ValueError as exc:
        print(c("red", str(exc)), file=sys.stderr)
        return 1
    if public_url and not host:
        host = "127.0.0.1"
    program, err = hub_program(
        host, port if flags.get("--port") else None, public_url,
        unsafe_any=bool(flags.get("--unsafe-bind-any")),
    )
    if err:
        print(c("red", err), file=sys.stderr)
        return 1
    # A hub started from a shell would keep the port, and the service's
    # copy would fail to bind on every retry.
    probe_host = host or _tailscale_ip() or "127.0.0.1"
    loaded = (launchd.loaded_pid()[0] if darwin
              else systemd.status(old_unit=systemd.OLD_UNIT)[1])
    stray = not loaded and _probe(probe_host, port, timeout=1.0) is not None
    log_path = remote_dir_for() / HUB.log_name
    try:
        path, note = install_service(program, log_path)
    except RuntimeError as exc:
        print(c("red", str(exc)), file=sys.stderr)
        return 1
    _print_installed(path, program, log_path, note)
    if not stray:
        # A hub that can't start exits and launchd only retries, so wait
        # for it to answer rather than leave the phone offline unsaid.
        for _ in range(_INSTALL_WAIT_S):
            if _probe(probe_host, port, timeout=1.0) is not None:
                print(c("green", f"  hub answering on {probe_host}:{port}"))
                break
            time.sleep(1)
        else:
            print(c("yellow", f"the hub is not answering on {probe_host}:{port} after "
                              f"{_INSTALL_WAIT_S} s; see {log_path}"), file=sys.stderr)
    if stray:
        manager = "launchd" if darwin else "systemd"
        delay = launchd.THROTTLE_S if darwin else systemd.RESTART_S
        print(c("yellow",
                f"a hub started outside {manager} already answers on {probe_host}:{port}. "
                f"Stop it (pgrep -f 'swe remote serve') and {manager} starts its own "
                f"within {delay} s."),
              file=sys.stderr)
    return 0


def _cmd_pair(flags: dict, json_view: bool) -> int:
    try:
        host, port, scheme = _phone_address(flags)
    except ValueError as exc:
        print(c("red", str(exc)), file=error_stream(json_view))
        return 1
    name = flags.get("--name") or ""
    token = pairing.mint_pair_token()
    url = pairing.pair_url(host, port, token, name, scheme)
    payload = pairing.pair_payload(host, port, token, name, scheme)
    if json_view:
        return emit({
            "host": host,
            "port": port,
            "token": token,
            "name": name,
            "url": url,
            "deep_link": payload,
            "expires_in_s": int(pairing.TOKEN_TTL_S),
        })
    print(f"Pair a device ({int(pairing.TOKEN_TTL_S / 60)} min window):\n")
    print(f"  {c('cyan', url)}")
    print(f"  {c('dim', payload)}\n")
    qrencode = shutil.which("qrencode")
    if qrencode:
        try:
            # --app encodes the quiver:// deep link so the Camera app
            # opens the iOS app instead of the browser web app.
            qr = payload if flags.get("--app") else url
            subprocess.run(
                [qrencode, "-t", "ANSIUTF8", qr], check=False, timeout=10
            )
            print()
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        print(c("dim", "  (install qrencode for a QR; the URL works too)"))
    if name:
        print(c("dim", f"  device name on exchange: {name}"))
    print(c("dim", f"  raw token: {token}"))
    return 0


def _cmd_status(flags: dict, json_view: bool) -> int:
    try:
        host, port = _host_port(flags)
    except ValueError as exc:
        print(c("red", str(exc)), file=error_stream(json_view))
        return 1
    saved = _saved_hub(flags)
    public_url = str(saved.get("public_url") or "")
    if saved.get("host") and isinstance(saved.get("port"), int):
        # Probe where the hub listens: behind serve that is loopback,
        # which the tailnet IP default would miss.
        host = saved["host"]
        port = saved["port"]
        if host in WILDCARD_HOSTS:
            host = "127.0.0.1"
    if not host:
        host = _tailscale_ip() or "127.0.0.1"
    info = _probe(host, port)
    devices = pairing.load_devices()
    machines = catalog_mod.load_machines()
    if json_view:
        emit({
            "hub": info or {"ok": False},
            "hub_addr": f"{host}:{port}",
            "public_url": public_url or None,
            "machines": list(machines.values()),
            "devices": [
                pairing.device_view(did, row)
                for did, row in devices.items()
            ],
        })
        # The envelope goes out either way; the exit code still reports
        # reachability, like the pretty path below (docs/json-contracts.md).
        return 0 if info is not None else 1
    if info is None:
        print(c("red", f"hub unreachable at {host}:{port}"))
        print(c("dim", f"  start it: swe remote serve --host={host}"))
    else:
        # /v1/status answers only {ok, v} — it is a reachability check,
        # not a description of the hub. The local hub's machine row and
        # version come from this machine's own files instead; a remote
        # hub's fields stay shown when an older one still sends them.
        local = (
            not flags.get("--host")
            or host in {"127.0.0.1", "localhost", "::1"}
        )
        machine = info.get("machine") or {}
        version = str(info.get("version") or "")
        if local and not machine:
            machine = catalog_mod.local_machine()
        if local and not version:
            version = __version__
        line = f"hub {c('green', 'online')} at {c('cyan', f'{host}:{port}')}"
        uptime = info.get("uptime_s")
        if uptime is not None:
            line += f" · up {uptime}s"
        if version:
            line += f" · swe {version}"
        print(line)
        if machine:
            print(f"  machine: {machine.get('name', '?')} "
                  f"({machine.get('id', '?')})")
        if public_url:
            print(f"  phones: {c('cyan', public_url)}")
    print(f"  devices paired: {len(devices)}")
    if machines:
        print("  machines:")
        for mid, row in machines.items():
            state = c("green", "online") if row.get("online") else c("dim", "offline")
            print(f"    {row.get('name', mid)} {state} ({mid})")
    return 0 if info is not None else 1


_AUTO_APPROVE_NOTE = (
    "requests from the hub owner's own untagged tailnet devices pair "
    "without a code"
)


def _cmd_devices(flags: dict, json_view: bool) -> int:
    auto = flags.get("--auto-approve")
    if auto is not None:
        if auto not in {"on", "off"}:
            print(c("red", "--auto-approve takes on or off"),
                  file=error_stream(json_view))
            return 1
        # One action per run: a toggle that also swallowed --revoke would
        # report success while the device stays active.
        others = [f for f in ("--requests", "--approve", "--deny",
                              "--request-id", "--revoke") if f in flags]
        if others:
            print(c("red", f"--auto-approve can't be combined with "
                           f"{', '.join(others)}; run them separately"),
                  file=error_stream(json_view))
            return 1
        on = pairing.set_auto_approve_owner(None, auto == "on")
        if json_view:
            return emit({"auto_approve_owner": on})
        if on:
            print(f"owner auto-approve {c('green', 'on')}: "
                  f"{_AUTO_APPROVE_NOTE}")
        else:
            print(f"owner auto-approve {c('dim', 'off')}: every request "
                  "needs its code")
        return 0
    if flags.get("--requests"):
        rows = pairing.list_requests()
        auto_on = pairing.auto_approve_owner()
        if json_view:
            return emit({"requests": rows, "auto_approve_owner": auto_on})
        state = c("green", "on") if auto_on else c("dim", "off")
        print(c("dim", "owner auto-approve: ") + state
              + c("dim", "  (swe remote devices --auto-approve=on|off)"))
        if not rows:
            print(c("dim", "no pending pair requests"))
            return 0
        for r in rows:
            print(
                f"  {r['request_id']}  {r.get('name', 'device')}  "
                f"{r.get('node_name') or '?'}  "
                f"{r.get('login') or 'no login'}  "
                f"{r.get('peer_ip', '?')}  expires in {r['expires_in_s']}s"
            )
        return 0
    # --request-id holds the code to one row, as the hub's HTTP route
    # does: a code typed against one request never approves another,
    # and wrong approve codes count toward that row's lockout.
    request_id = flags.get("--request-id")
    if request_id == "":
        # Empty would quietly widen the code to every pending row.
        print(c("red", "--request-id needs an id from --requests"),
              file=error_stream(json_view))
        return 1
    if request_id is not None and (
        flags.get("--approve") is None and flags.get("--deny") is None
    ):
        print(c("red", "--request-id goes with --approve=<code> or "
                       "--deny=<code>"),
              file=error_stream(json_view))
        return 1
    approve = flags.get("--approve")
    if approve is not None:
        code = "".join(ch for ch in approve if ch.isdigit())
        res = pairing.approve_request(None, code, request_id)
        if json_view:
            if res is None:
                emit({"ok": False})
                return 1
            return emit({**res, "ok": True})
        if res is None:
            print(c("red", "no pending request matches that code"),
                  file=error_stream(json_view))
            return 1
        print(
            f"approved {res['name']} ({res.get('node_name', '?')}) "
            f"→ device {res['device_id']}"
        )
        return 0
    deny = flags.get("--deny")
    if deny is not None:
        code = "".join(ch for ch in deny if ch.isdigit())
        ok = pairing.deny_request(None, code, request_id)
        if json_view:
            return emit({"ok": ok})
        if not ok:
            print(c("red", "no pending request matches that code"),
                  file=error_stream(json_view))
            return 1
        print("denied")
        return 0
    revoke = flags.get("--revoke")
    if revoke:
        try:
            if revoke.startswith(devices_mod.TAILNET_PREFIX):
                # One of the owner's tailnet devices, let in without
                # pairing: it stops getting in until it pairs again.
                ok = pairing.revoke_signin(
                    None, revoke[len(devices_mod.TAILNET_PREFIX):])
            else:
                ok = pairing.revoke_device(None, revoke)
        except sshkeys.KeysNotSynced as exc:
            print(c("red", f"revoked {revoke}, but its SSH key is still in "
                           f"~/.ssh/authorized_keys: {exc}"),
                  file=error_stream(json_view))
            return 1
        except OSError as exc:
            print(c("red", f"could not revoke {revoke}: {exc}"),
                  file=error_stream(json_view))
            return 1
        if json_view:
            return emit({"revoked": revoke, "ok": ok})
        if ok:
            print(f"revoked {revoke}")
            return 0
        print(c("red", f"no such device: {revoke}"),
              file=error_stream(json_view))
        return 1
    devices = pairing.load_devices()
    signins = pairing.load_signins()
    if json_view:
        return emit({
            "devices": [
                pairing.device_view(did, row)
                for did, row in devices.items()
            ],
            "signins": devices_mod.signin_rows(None, None),
        })
    _print_signins(signins)
    if not devices:
        print(c("dim", "no paired devices — swe remote pair"))
        return 0
    for did, row in devices.items():
        mark = c("red", "revoked") if row.get("revoked_at") else c("green", "active")
        seen = row.get("last_seen_at")
        seen_s = time.strftime("%Y-%m-%d %H:%M", time.localtime(seen)) if seen else "never"
        label = str(
            row.get("display_name") or row.get("name") or "device"
        )
        bits = [
            str(row[k]) for k in ("kind", "client", "os", "node_name")
            if row.get(k)
        ]
        detail = f"  {' · '.join(bits)}" if bits else ""
        print(f"  {did}  {label}  {mark}{detail}  last seen {seen_s}")
    return 0


def _print_signins(signins: dict) -> None:
    """The owner's tailnet devices the hub let in without pairing, each
    revocable with ``--revoke=tailnet:<node>``."""
    if not signins:
        return
    print(c("dim", "signed in through Tailscale (revoke with --revoke=tailnet:<node>):"))
    for node, row in signins.items():
        mark = c("red", "revoked") if row.get("revoked_at") else c("green", "active")
        seen = row.get("last_seen_at")
        seen_s = time.strftime("%Y-%m-%d %H:%M", time.localtime(seen)) if seen else "never"
        print(f"  tailnet:{node}  {row.get('node_name') or 'device'}  {mark}  last seen {seen_s}")
    print(c("dim", "paired devices:"))


def _cmd_attach(flags: dict, rest: list[str]) -> int:
    """Local attach: mark + relaunch; the hub reads the registry."""
    if not rest:
        print(c("red", "attach needs a session key or id"),
              file=sys.stderr)
        return 1
    if len(rest) > 1:
        return _unexpected(rest[1:])
    want = rest[0]
    mode = flags.get("--mode") or "auto"
    if mode not in supervise.ATTACH_MODES:
        print(c("red", "--mode must be one of "
                + ", ".join(sorted(supervise.ATTACH_MODES))),
              file=sys.stderr)
        return 1
    from swe.sessions.aggregator import get_all_sessions
    from swe.sessions.status import session_statuses

    sessions = get_all_sessions(limit=None, use_cache=True)
    matches = [
        s for s in sessions
        if protocol.session_key(s.tool_name, s.session_id, s.path) == want
        or s.session_id == want
    ]
    if not matches:
        # Permit a unique substring match on the session id.
        matches = [s for s in sessions if want in (s.session_id or "")]
    if not matches:
        print(c("red", f"no session matches {want}"), file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(c("red", f"{want} matches {len(matches)} sessions; use the full key"),
              file=sys.stderr)
        return 1
    session = matches[0]
    key = protocol.session_key(session.tool_name, session.session_id, session.path)
    status = session_statuses([session])[0]
    transcript = tail.resolve_jsonl_path(session)
    last_append = None
    try:
        last_append = transcript.stat().st_mtime if transcript else None
    except OSError:
        pass
    tuning = {"model": flags.get("--model") or "",
              "effort": flags.get("--effort") or ""}
    try:
        model = protocol._launch_value(tuning, "model")
        effort = protocol._launch_value(tuning, "effort")
    except ValueError as exc:
        print(c("red", str(exc)), file=sys.stderr)
        return 1
    ok, note = supervise.attach(
        None, key, session,
        last_append_at=last_append, status=status, mode=mode,
        model=model, effort=effort,
    )
    if not ok:
        print(c("red", note), file=sys.stderr)
        return 1
    print(f"{c('green', 'attached')} {key} — {note}")
    return 0


def _agent_conf(flags: dict) -> dict | None:
    """The satellite's hub and credential: paired now when ``--token``
    is given, else the saved ones (``--hub`` overrides the URL). Prints
    why and returns None when there is none."""
    hub = (flags.get("--hub") or "").rstrip("/")
    token = flags.get("--token") or ""
    conf = agent_mod.load_agent()
    if token:
        if not hub:
            print(c("red", "--token needs --hub=http://<host>:<port>"),
                  file=sys.stderr)
            return None
        conf = agent_mod.pair_agent(
            hub, token, flags.get("--name") or ""
        )
        if conf is None:
            print(c("red", "pairing failed — check the token and hub"),
                  file=sys.stderr)
            return None
        print(c("green", f"paired with {conf['hub']}"))
    elif hub:
        conf = dict(conf or {}, hub=hub)
    if not conf or not conf.get("credential") or not conf.get("hub"):
        print(c("red",
                "no hub credential — run: "
                "swe remote agent --hub=http://<host>:<port> --token=T"),
              file=sys.stderr)
        return None
    return conf


def agent_service_report(home: Path | None = None, run=subprocess.run) -> dict:
    """``swe remote agent --status --json``: is this machine a paired
    satellite, and is its background service installed and running."""
    conf = agent_mod.load_agent(home) or {}
    problem = _service_problem(AGENT)
    path = next((p for p in service_files(AGENT, home) if p.exists()),
                service_file(AGENT, home))
    args, running = service_status(AGENT, home, run) if problem is None else (None, False)
    return {
        "configured": bool(conf.get("hub") and conf.get("credential")),
        "hub": conf.get("hub") or None,
        "supported": problem is None,
        "problem": problem,
        "installed": path.exists(),
        "running": running,
        "service": str(path),
        "program": args,
        "log": str(remote_dir_for(home) / AGENT.log_name),
        "hub_installed": any(p.exists() for p in service_files(HUB, home)),
    }


def _cmd_agent_status(json_view: bool) -> int:
    report = agent_service_report()
    if json_view:
        return emit(report)
    if report["running"]:
        state = c("green", "running")
    elif report["installed"]:
        state = c("yellow", "installed, not running")
    else:
        state = c("dim", "not installed")
    print(f"remote agent {state}")
    print(f"  hub      {report['hub'] or c('dim', 'not paired (swe remote agent --hub=URL --token=T)')}")
    print(f"  service  {report['service']}")
    if report["program"]:
        print(f"  runs     {' '.join(report['program'])}")
    print(f"  log      {report['log']}")
    if report["problem"]:
        print(c("yellow", f"  {report['problem']}"))
    elif report["hub_installed"]:
        print(c("yellow", "  this machine's hub is installed here, so --install refuses the agent"))
    return 0


def _cmd_agent_service(flags: dict) -> int:
    """``agent --install`` / ``--uninstall``: the satellite as a
    LaunchAgent or systemd user unit, apart from the hub's."""
    problem = _service_problem(AGENT)
    if problem:
        print(c("red", problem), file=sys.stderr)
        return 1
    if flags.get("--uninstall"):
        return _cmd_uninstall_service(AGENT)
    # Checked before pairing, so a refused install spends no token.
    refusal = service_conflict(AGENT)
    if refusal:
        print(c("red", refusal), file=sys.stderr)
        return 1
    # A shell-started agent would run every command a second time beside
    # the service's; the service's own agent is the one a reinstall
    # restarts.
    holder = agent_mod.lock_holder()
    if holder is not None and not service_status(AGENT)[1]:
        pid = f" (pid {holder})" if holder else ""
        print(c("red", f"a swe remote agent started outside the service already runs "
                       f"here{pid}; stop it, then run --install again"), file=sys.stderr)
        return 1
    if _agent_conf(flags) is None:
        return 1
    swe, err = _service_swe(AGENT)
    if err:
        print(c("red", err), file=sys.stderr)
        return 1
    # The service reads the credential from agent.json, so no secret
    # lands in the plist or unit. A --hub without --token overrides the
    # saved URL for this run only, so the service has to carry it.
    program = [swe, "remote", "agent"]
    if flags.get("--hub") and not flags.get("--token"):
        program.append(f"--hub={flags['--hub'].rstrip('/')}")
    log_path = remote_dir_for() / AGENT.log_name
    try:
        path, note = install_service(program, log_path, service=AGENT)
    except RuntimeError as exc:
        print(c("red", str(exc)), file=sys.stderr)
        return 1
    _print_installed(path, program, log_path, note)
    print(c("dim", "  check it with `swe remote agent --status`"))
    return 0


def _cmd_agent(flags: dict, json_view: bool = False) -> int:
    """Satellite: pair on first run (``--token``), then dial the hub."""
    if flags.get("--status"):
        return _cmd_agent_status(json_view)
    if flags.get("--install") or flags.get("--uninstall"):
        if not json_view:
            return _cmd_agent_service(flags)
        # The app reads the status afterwards from stdout, so the prose
        # (and any refusal) goes to stderr.
        with contextlib.redirect_stdout(sys.stderr):
            rc = _cmd_agent_service(flags)
        return emit(agent_service_report()) if rc == 0 else rc
    # One agent per ~/.swe: two would each run every command the hub
    # queues. Under launchd or systemd this exits and the service retries.
    # Taken before pairing, so a refused start leaves agent.json alone.
    lock, holder = agent_mod.take_lock()
    if lock is None:
        pid = f" (pid {holder})" if holder else ""
        print(c("red", f"{_stamp()}  another swe remote agent already runs here{pid}; "
                       "stop it first"), file=sys.stderr, flush=True)
        return 1
    conf = _agent_conf(flags)
    if conf is None:
        lock.close()
        return 1
    machine = catalog_mod.local_machine()
    print(
        f"remote agent {c('cyan', conf['hub'])} "
        f"as {c('bold', machine['name'])} ({machine['id']})"
    )
    return agent_mod.serve(conf["hub"], conf["credential"])


def _cmd_notify(flags: dict, json_view: bool) -> int:
    """Configure the ntfy fan-out the running hub reads at startup."""
    updates = {}
    if "--ntfy" in flags:
        updates["url"] = flags["--ntfy"]
    if "--topic" in flags:
        updates["topic"] = flags["--topic"]
    if "--ntfy-token" in flags:
        updates["ntfy_token"] = flags["--ntfy-token"]
    if "--on" in flags:
        updates["enabled"] = True
    if "--off" in flags:
        updates["enabled"] = False
    if "--service-status" in flags:
        value = flags["--service-status"]
        if value not in ("on", "off"):
            print(c("red", "--service-status takes on or off"), file=sys.stderr)
            return 1
        updates["service_status"] = value == "on"
    if "--ntfy" in flags or "--topic" in flags:
        updates.setdefault("enabled", True)  # configuring implies on
    cfg = notify.configure(None, **updates) if updates else \
        notify.load_config()
    if json_view:
        redacted = dict(cfg)
        for k in ("ntfy_token", "action_credential"):
            if redacted.get(k):
                redacted[k] = "***"
        return emit({"notify": redacted})
    if not cfg.get("url") or not cfg.get("topic"):
        print(c("dim", "ntfy not configured — swe remote notify "
                      "--ntfy=<url> --topic=<t>"))
        return 0
    state = c("green", "on") if cfg.get("enabled") else c("dim", "off")
    print(f"ntfy {state}  {cfg['url']}/{cfg['topic']}")
    if not cfg.get("enabled"):
        print(c("dim", "  enable: swe remote notify --on"))
    status = "on" if cfg.get("service_status") is True else "off"
    print(c("dim", f"  service status pushes {status} (--service-status=on|off)"))
    print(c("dim", "  changes apply on the next `swe remote serve`"))
    return 0


_SETUP_MARK = {
    setup_mod.OK: ("green", "✓"),
    setup_mod.TODO: ("cyan", "○"),
    setup_mod.NEEDS_YOU: ("yellow", "!"),
    setup_mod.FAILED: ("red", "✗"),
    setup_mod.BLOCKED: ("dim", "·"),
}


def _cmd_setup(flags: dict, json_view: bool) -> int:
    """``swe remote setup``: every step from nothing to a paired phone."""
    try:
        port = int(flags["--port"]) if flags.get("--port") else None
    except ValueError:
        port = 0
    if port is not None and not 1 <= port <= 65535:
        # Checked before anything changes: --apply would stop a working
        # hub and install one that can't start.
        print(c("red", "--port must be a number from 1 to 65535"), file=error_stream(json_view))
        return 1
    if flags.get("--apply"):
        say = (lambda _m: None) if json_view else (lambda m: print(c("dim", f"… {m}"), flush=True))
        report = setup_mod.apply(port=port, log=say, fresh_cert=bool(flags.get("--recheck")))
    else:
        report = setup_mod.evaluate(setup_mod.gather(
            port=port, fetch_cert=bool(flags.get("--recheck")),
            fresh_cert=bool(flags.get("--recheck"))))
    if json_view:
        return emit(report)
    for step in report["steps"]:
        color, mark = _SETUP_MARK.get(step["state"], ("dim", "?"))
        line = f"  {c(color, mark)} {step['title']}"
        if step["detail"]:
            line += c("dim", f"  {step['detail']}")
        print(line)
        action = step.get("action") or {}
        if step["state"] in {setup_mod.NEEDS_YOU, setup_mod.FAILED}:
            if action.get("url"):
                print(c("dim", f"      open {action['url']}"))
            elif action.get("command"):
                print(c("dim", f"      run  {action['command']}"))
            elif action.get("kind") == "launch":
                print(c("dim", f"      open the {action.get('app')} app"))
            elif action.get("kind") == "pair":
                print(c("dim", "      run  swe remote pair --app"))
    if report["ready"]:
        print(c("green", f"\nReady: phones reach this hub at {report['url']}"))
        return 0
    if any(s["state"] == setup_mod.TODO for s in report["steps"]):
        print(c("dim", "\nRun `swe remote setup --apply` to do the steps marked ○."))
    return 0 if not flags.get("--apply") else 1


def _cmd_ssh(flags: dict, json_view: bool) -> int:
    """The phone SSH console's setup on this machine: on/off, sshd,
    host keys, and which devices' keys are in authorized_keys."""
    if "--on" in flags and "--off" in flags:
        print(c("red", "pick one of --on and --off"),
              file=error_stream(json_view))
        return 1
    try:
        if "--on" in flags or "--off" in flags:
            sshkeys.set_enabled(None, "--on" in flags)
        else:
            sshkeys.sync(None)
    except OSError as exc:
        print(c("red", f"could not update ~/.ssh/authorized_keys: {exc}"),
              file=error_stream(json_view))
        return 1
    view = sshkeys.view(None, None)
    devices = pairing.load_devices()
    keyed = [
        {
            "id": did,
            "name": row.get("name") or "device",
            "fingerprint": sshkeys.fingerprint(key),
            "installed": sshkeys.installed_line(None, did),
            "revoked": bool(row.get("revoked_at")),
        }
        for did, row in devices.items()
        if (key := sshkeys.normalize_public_key(row.get("ssh_public_key")))
    ]
    if json_view:
        return emit({
            "enabled": view["enabled"],
            "user": view["user"],
            "port": view["port"],
            "listening": view["listening"],
            "host_keys": [
                {"key": k, "fingerprint": sshkeys.fingerprint(k)}
                for k in view["host_keys"]
            ],
            "devices": keyed,
        })
    state = c("green", "on") if view["enabled"] else c("dim", "off")
    print(f"phone ssh {state}  logs in as {view['user']} on port "
          f"{view['port']}")
    if not view["enabled"]:
        print(c("dim", "  turn on: swe remote ssh --on"))
    if not view["listening"]:
        hint = ("System Settings, General, Sharing, Remote Login"
                if sys.platform == "darwin" else "start sshd")
        print(c("yellow", f"  nothing listens on port {view['port']}: {hint}"))
    if not view["host_keys"]:
        print(c("yellow", f"  no ed25519 or ecdsa host key in "
                         f"{sshkeys.HOST_KEY_DIR}: the phone has nothing to pin"))
    for key in view["host_keys"]:
        print(c("dim", f"  host key {sshkeys.fingerprint(key)} "
                       f"({key.split()[0]})"))
    if not keyed:
        print(c("dim", "  no device has sent a key yet: open the console "
                       "in SWE Remote"))
    for row in keyed:
        if row["revoked"]:
            mark = c("red", "revoked")
        elif row["installed"]:
            mark = c("green", "installed")
        else:
            mark = c("dim", "not installed")
        print(f"  {row['id']}  {row['name']}  {mark}  {row['fingerprint']}")
    return 0


def _remote_state_dir() -> str:
    """The remote state dir for an error line. ``remote_dir_for`` is
    itself what dies when the home can't be resolved, so the handler
    falls back to the ``~/`` spelling rather than raise inside it."""
    try:
        return str(remote_dir_for())
    except Exception:
        return "~/.swe/data/remote"


def _unexpected(extra: list[str]) -> int:
    """The refusal for trailing positionals a subcommand doesn't take."""
    print(c("red", f"unexpected args: {' '.join(extra)}"), file=sys.stderr)
    return 1


def _dispatch(sub: str, flags: dict, rest: list[str], json_view: bool) -> int:
    if sub == "attach":
        return _cmd_attach(flags, rest)
    # Every other subcommand takes flags only.
    handlers = {
        "serve": lambda: _cmd_serve(flags),
        "pair": lambda: _cmd_pair(flags, json_view),
        "status": lambda: _cmd_status(flags, json_view),
        "devices": lambda: _cmd_devices(flags, json_view),
        "agent": lambda: _cmd_agent(flags, json_view),
        "setup": lambda: _cmd_setup(flags, json_view),
        "notify": lambda: _cmd_notify(flags, json_view),
        "ssh": lambda: _cmd_ssh(flags, json_view),
    }
    handler = handlers.get(sub)
    if handler is None:
        return 0
    if rest:
        return _unexpected(rest)
    return handler()


def cmd_remote(args: list[str]) -> int:
    try:
        sub, flags, rest, json_view = _parse_flags(list(args))
    except ValueError as exc:
        print(c("red", str(exc)), file=sys.stderr)
        return 1
    if not sub or sub not in _SUBCOMMANDS:
        print(_USAGE)
        if sub:
            print(c("red", f"unknown subcommand: {sub}"), file=sys.stderr)
            return 1
        return 0
    try:
        return _dispatch(sub, flags, rest, json_view)
    except OSError as exc:
        # Every subcommand's write lands under ~/.swe/data/remote
        # (pairing._state_lock mkdirs it, journals and registries sit in
        # it), so an unset, empty or read-only HOME surfaces here as one
        # bare OSError — but `serve --install` also writes under
        # ~/Library/LaunchAgents or ~/.config/systemd/user, so name the
        # path that actually failed, not the state dir it usually is.
        print(c("red", f"can't write {exc.filename or _remote_state_dir()}: "
                       f"{exc.strerror or exc}"),
              file=error_stream(json_view))
        return 1
    except RuntimeError as exc:
        # HOME unset and the uid has no passwd entry: Path.home() gives
        # up with "Could not determine home directory" — the same dead
        # end as an unwritable one. Any other RuntimeError is a bug and
        # keeps its traceback.
        if "home directory" not in str(exc):
            raise
        print(c("red", f"can't write ~/.swe/data/remote: {exc}"),
              file=error_stream(json_view))
        return 1
