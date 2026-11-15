"""Who holds the far end of a loopback connection: the check that lets
the hub believe ``tailscale serve``'s forwarded-client headers.

Behind ``--public-url`` the hub binds 127.0.0.1 and ``tailscale serve``
dials it there, so every request arrives from a loopback peer. Any other
local process can dial the same port and write its own
``X-Forwarded-For``, so a loopback peer alone proves nothing (#965).
``is_proxy`` asks the OS which process owns the other end of the
connection and says yes only for the Tailscale daemon:

- Linux: ``/proc/net/tcp`` (world-readable) gives every socket's owner
  uid. tailscaled runs as root, and root could forge anything anyway.
- macOS: ``netstat -anv`` names the pid behind every socket, root's
  included, without privileges; ``ps`` gives that pid's uid from the
  kernel and ``proc_pidpath`` its executable, so a process can't rename
  itself into the daemon. The pid must run as root AND be the daemon's
  own executable: the Standalone variant's system extension under
  ``/Library/SystemExtensions``, the open-source ``tailscaled``, or the
  App Store variant's network extension at its exact path inside a
  root-owned ``/Applications/Tailscale.app``. No other executable in the
  bundle counts, root or not (the app itself, its updater, widgets):
  none of them is the daemon.

Both read only the row for exactly this connection, and only while it
is ESTABLISHED: the owner either table shows for a socket that is
closing or closed (FIN_WAIT, TIME_WAIT and the rest) is not reliable.
Two rows for one connection, a row cut short, or a pid that isn't a
plain positive number all read as "can't tell".

Anything else, or any failure to tell, is "no". The request is then
read as the loopback peer it is: it loses the tailnet identity and
keeps everything a bearer credential gives.

The lookup can be blind (#1944). On macOS 27 a process that is
ad-hoc signed, or has a live ad-hoc-signed ancestor, sees an empty TCP
table: ``netstat -anv`` prints no sockets, so no peer is ever placed.
Homebrew's Python and Nix's shells are ad-hoc signed; a
Developer-ID-signed Python, such as python.org's, started by launchd or
a signed shell sees the table. A blind hub still fails closed: tailscaled
is never recognised, and every request through ``tailscale serve``
reads as loopback. ``owner_lookup_works`` tells a blind host apart so
``swe remote serve`` can say so at start; it never changes what
``is_proxy`` accepts, and an empty table is never read as anything but
"can't tell".
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import struct
import subprocess
import sys
from pathlib import Path
from typing import Callable

Addr = tuple[str, int]

_NETSTAT = "/usr/sbin/netstat"
_PS = "/bin/ps"
_TAILSCALE_APP = "/Applications/Tailscale.app"
# The daemon's executables on macOS, as ``proc_pidpath`` reports them.
# The Standalone variant's extension runs from where macOS installed it,
# a SIP-protected folder named by a UUID; the copy the app stages inside
# its own bundle is not the running one and never matches.
_SYSTEM_EXTENSION = re.compile(
    r"/Library/SystemExtensions/[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}"
    r"/io\.tailscale\.ipn\.macsys\.network-extension\.systemextension"
    r"/Contents/MacOS/io\.tailscale\.ipn\.macsys\.network-extension")
# The App Store variant's network extension inside the app bundle.
_APP_STORE_EXTENSION = (_TAILSCALE_APP
                        + "/Contents/PlugIns/IPNExtension.appex/Contents/MacOS/IPNExtension")
# The open-source daemon, wherever it was installed.
_TAILSCALED = "tailscaled"
# Linux ``/proc/net/tcp`` ``st`` and macOS ``netstat`` state of a live
# connection. Every other state is a socket on its way out.
_PROC_ESTABLISHED = "01"
_NETSTAT_ESTABLISHED = "ESTABLISHED"
# Largest pid either kernel hands out (pid_t is a signed 32-bit int).
_PID_MAX = 2 ** 31 - 1
# ``process:pid``: the digits after the last colon of a token. The name
# is the process's own choice and can hold spaces and ``:digits`` of its
# own (``x:1 y`` prints as ``x:1 y:<pid>``), so only the last such token
# on the line is the pid; the columns after it hold no colons.
_PROC_PID = re.compile(r".*:(\d+)")


def is_proxy(server: Addr, peer: Addr) -> bool:
    """True when the socket at ``peer``, connected to ``server``, belongs
    to the Tailscale daemon (see the module docstring)."""
    try:
        if sys.platform.startswith("linux"):
            return linux_uid(server, peer) == 0
        if sys.platform == "darwin":
            return _darwin_is_proxy(server, peer)
    except Exception:  # noqa: BLE001 - any doubt is "not the proxy"
        return False
    return False


def _norm(ip: str) -> str:
    """One spelling per address: ``::ffff:127.0.0.1`` reads as
    ``127.0.0.1``, and v6 is compressed."""
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return ip
    mapped = getattr(addr, "ipv4_mapped", None)
    return str(mapped or addr)


def _same(a: Addr, b: Addr) -> bool:
    return a[1] == b[1] and _norm(a[0]) == _norm(b[0])


# -- Linux --------------------------------------------------------------


def _proc_addr(field: str) -> Addr | None:
    """``0100007F:CE2C`` as ``("127.0.0.1", 52780)``: the kernel prints
    each 32-bit word of the address in host byte order."""
    hexip, _, hexport = field.partition(":")
    try:
        port = int(hexport, 16)
        if len(hexip) == 8:
            raw = struct.pack("=I", int(hexip, 16))
            return socket.inet_ntop(socket.AF_INET, raw), port
        if len(hexip) == 32:
            words = [int(hexip[i:i + 8], 16) for i in range(0, 32, 8)]
            raw = struct.pack("=4I", *words)
            return socket.inet_ntop(socket.AF_INET6, raw), port
    except (ValueError, OSError, struct.error):
        return None
    return None


def linux_uid(server: Addr, peer: Addr, root: Path = Path("/proc/net")) -> int | None:
    """The uid owning the ESTABLISHED socket whose local end is ``peer``
    and remote end ``server``, or None when no such socket is listed,
    when it is in any other state, or when the tables list it more than
    once."""
    rows = []
    for name in ("tcp", "tcp6"):
        try:
            lines = (root / name).read_text(encoding="ascii").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 3:
                continue
            local, remote = _proc_addr(fields[1]), _proc_addr(fields[2])
            if local and remote and _same(local, peer) and _same(remote, server):
                rows.append(fields)
    # One live connection is one row. A row cut short still counts, so
    # it makes the answer doubtful rather than vanishing.
    if len(rows) != 1:
        return None
    fields = rows[0]
    if len(fields) < 8 or fields[3] != _PROC_ESTABLISHED:
        return None
    uid = fields[7]
    return int(uid) if uid.isascii() and uid.isdigit() else None


# -- macOS --------------------------------------------------------------


def _bsd_addr(field: str) -> Addr | None:
    """``127.0.0.1.52780`` or ``::1.8477`` as ``(ip, port)``."""
    host, _, port = field.rpartition(".")
    if not host or not port.isdigit():
        return None
    return host, int(port)


def netstat_pid(server: Addr, peer: Addr, text: str) -> int | None:
    """The pid ``netstat -anv -p tcp`` names for the ESTABLISHED socket
    whose local end is ``peer`` and foreign end ``server``, or None when
    no such row is listed, when it is in any other state, when the table
    lists it more than once, or when its pid column isn't a pid."""
    rows = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 5 or not fields[0].startswith("tcp"):
            continue
        local, foreign = _bsd_addr(fields[3]), _bsd_addr(fields[4])
        if local and foreign and _same(local, peer) and _same(foreign, server):
            rows.append(fields)
    # One live connection is one row. A row cut short still counts, so
    # it makes the answer doubtful rather than vanishing.
    if len(rows) != 1:
        return None
    fields = rows[0]
    if len(fields) < 7 or fields[5] != _NETSTAT_ESTABLISHED:
        return None
    for tok in reversed(fields[6:]):
        m = _PROC_PID.fullmatch(tok)
        if m:
            digits = m.group(1)
            if not digits.isascii() or digits.startswith("0"):
                return None
            pid = int(digits)
            return pid if pid <= _PID_MAX else None
    return None


def _run(argv: list[str]) -> str:
    cp = subprocess.run(argv, capture_output=True, text=True, timeout=3,
                        stdin=subprocess.DEVNULL)
    return cp.stdout if cp.returncode == 0 else ""


def _pid_uid(pid: int) -> int | None:
    out = _run([_PS, "-o", "uid=", "-p", str(pid)]).strip()
    return int(out) if out.isdigit() else None


def _pid_path(pid: int) -> str:
    """The pid's executable as the kernel knows it (``proc_pidpath``)."""
    import ctypes

    lib = ctypes.CDLL("/usr/lib/libproc.dylib")
    buf = ctypes.create_string_buffer(4096)
    n = lib.proc_pidpath(pid, buf, ctypes.c_uint32(len(buf)))
    return buf.value.decode("utf-8", "replace") if n > 0 else ""


def app_store_binary(path: str, app: str = _TAILSCALE_APP) -> bool:
    """True for a file inside a root-owned Tailscale.app that only root
    can change: a user can run what is there but not put code there.
    Says nothing about what the file is: ``daemon_executable`` decides
    which one is the daemon."""
    if not path.startswith(app.rstrip("/") + "/"):
        return False
    real = os.path.realpath(path)
    if not real.startswith(os.path.realpath(app) + "/"):
        return False
    try:
        bundle, exe = os.stat(app), os.stat(real)
    except OSError:
        return False
    return (bundle.st_uid == 0 and exe.st_uid == 0
            and not (bundle.st_mode & 0o022) and not (exe.st_mode & 0o022))


def daemon_executable(path: str) -> bool:
    """True when ``path`` (from ``proc_pidpath``) is the Tailscale
    daemon's own executable: the Standalone system extension, the App
    Store network extension in a root-owned bundle, or open-source
    ``tailscaled``. Never the app's own executable nor any other file
    in the bundle."""
    if not path.startswith("/") or "/../" in path or "/./" in path:
        return False
    if _SYSTEM_EXTENSION.fullmatch(path):
        return True
    if path == _APP_STORE_EXTENSION:
        return app_store_binary(path)
    if path.startswith(_TAILSCALE_APP.rstrip("/") + "/"):
        return False
    return os.path.basename(path) == _TAILSCALED


def _darwin_pid(server: Addr, peer: Addr) -> int | None:
    """The pid behind ``peer`` from a fresh ``netstat -anv -p tcp``."""
    return netstat_pid(server, peer, _run([_NETSTAT, "-anv", "-p", "tcp"]))


def _darwin_is_proxy(server: Addr, peer: Addr) -> bool:
    pid = _darwin_pid(server, peer)
    if pid is None or pid < 1 or pid == os.getpid():
        return False
    # Root AND the daemon's executable: either alone is not enough.
    if _pid_uid(pid) != 0:
        return False
    return daemon_executable(_pid_path(pid))


# -- Can the lookup see anything? ---------------------------------------


def owner_lookup_works(lookup: Callable[[Addr, Addr], int | None] | None = None,
                       me: int | None = None) -> bool:
    """True when this host's socket-owner lookup names this process as
    the owner of a loopback connection it holds itself.

    The lookup ``is_proxy`` relies on (``netstat`` on macOS, compared
    with this pid; ``/proc/net/tcp`` on Linux, compared with this uid)
    is asked about a connection opened here for the purpose. False means
    the lookup is blind on this host (see the module docstring) or can't
    be run: ``is_proxy`` then says no to every peer, tailscaled's
    included. Costs one socket pair
    and one lookup; never raises. Tests pass ``lookup`` and ``me``.
    Only a warning and a test skip read the answer.
    """
    try:
        if lookup is None:
            if sys.platform.startswith("linux"):
                lookup, me = linux_uid, os.getuid()
            elif sys.platform == "darwin":
                lookup, me = _darwin_pid, os.getpid()
            else:
                return False
        with socket.create_server(("127.0.0.1", 0)) as listener:
            listener.settimeout(3)
            server = listener.getsockname()[:2]
            with socket.create_connection(server, timeout=3) as client:
                conn, _ = listener.accept()
                with conn:
                    found = lookup(server, client.getsockname()[:2])
        return me is not None and found == me
    except Exception:  # noqa: BLE001 - a probe that fails is "can't see"
        return False
