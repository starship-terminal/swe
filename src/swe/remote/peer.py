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

The ``netstat`` table can be blind (#1944, #1950). On macOS 27 a
process that is ad-hoc or Apple-Development signed, or has a live
ancestor that is, sees an empty TCP table: ``netstat -anv`` prints no
sockets. Homebrew's Python, Nix's shells and a locally installed
SWE.app's bundled Python are all signed that way. When the table is
empty, and only then, the macOS check asks ``nettop`` instead
(NetworkStatistics, which such a process can still read):

- It finds the daemon's pids itself: every process whose executable
  (``proc_pidpath``) passes ``daemon_executable`` and that runs as
  root. Names never pick a process.
- For each, ``nettop -p <pid>`` lists that pid's TCP sockets. The pid
  holds the connection only when the listing has exactly one row for
  exactly this connection, local end the peer and far end the hub, and
  that row is ``Established``. The listing is read by its fixed shape:
  a known header, then that pid's own row, then socket rows, every row
  the header's width. Process names are the process's own choice and
  can hold commas, quotes or line breaks, so a listing that strays from
  the shape anywhere is "can't tell", never searched for a match.
- The pid's executable, uid and start time (to the microsecond, from
  ``kern.proc.pid``) are read before the listing and again after it,
  and must not change: a pid that exited and was reused meanwhile is
  "can't tell". More than one daemon pid holding the connection is too.

Whichever source names the pid, the same final check decides: root AND
the daemon's own executable, as above. A table ``netstat`` can see is
never second-guessed by ``nettop``: its "no" is the answer.

If neither can see (``nettop`` missing, or its output changed shape),
the hub still fails closed: tailscaled is never recognised, and every
request through ``tailscale serve`` reads as loopback.
``owner_lookup_works`` asks the same lookup, ``netstat`` then
``nettop``, about a connection of the hub's own, so ``swe remote
serve`` warns at start only when no source can see it; it never changes
what ``is_proxy`` accepts.

What this cannot tell apart: a connection the daemon itself opens on a
local caller's behalf really is the daemon's. Any daemon feature that
dials a local port for another local process (rather than for a tailnet
client) would reach the hub as the proxy. The CLI's relay
(``tailscale nc``) is not one: its connection is held by the CLI
process and is refused.
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
_NETTOP = "/usr/bin/nettop"
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
# ``nettop -L`` CSV: the leading columns this reads, by position. A
# listing whose header starts otherwise has changed shape.
_NETTOP_HEAD = ("time", "", "interface", "state")
_NETTOP_ESTABLISHED = "Established"
# A row's first column, ``11:40:36.050938``.
_NETTOP_TIME = re.compile(r"[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{1,9}")
# A socket row's second column: ``tcp4 127.0.0.1:52196<->127.0.0.1:8477``
# or ``tcp6 ::1.52198<->::1.8477`` (v6 separates the port with a dot).
_NETTOP_FLOW = re.compile(r"(tcp[46]) ([^<>\s]+)<->([^<>\s]+)")
# A process row's second column, ``<name>.<pid>``: the pid is the digits
# after the last dot, whatever dots the name holds.
_NETTOP_PROC = re.compile(r".*\.([1-9][0-9]*)")
# nettop takes about 0.05 s; a hung one is "can't tell", and soon.
_NETTOP_TIMEOUT = 2
# Hosts run one daemon. Past this many root daemon processes the
# answer is "can't tell" rather than a run of nettop calls.
_MAX_DAEMONS = 4
# ``struct kinfo_proc`` on 64-bit macOS, and where ``p_pid`` sits in it.
_KINFO_PROC_SIZE = 648
_KINFO_PID_OFFSET = 40
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


def _run(argv: list[str], timeout: float = 3) -> str:
    cp = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
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


def netstat_sees(text: str) -> bool:
    """True when ``netstat -anv -p tcp`` listed any TCP socket at all.
    A table that can see always lists the hub's own listener, so an
    empty one is a blind one (#1944), not an idle host."""
    return any(line.split(None, 1)[0].startswith("tcp")
               for line in text.splitlines() if line.strip())


def _nettop_addr(family: str, field: str) -> Addr | None:
    """``127.0.0.1:52780`` (tcp4) or ``::1.8477`` (tcp6) as ``(ip,
    port)``; None for ``*:*`` and anything else that is not one
    address of that family and a port."""
    host, _, port = field.rpartition(":" if family == "tcp4" else ".")
    if not port.isascii() or not port.isdigit() or not 0 < int(port) < 65536:
        return None
    try:
        version = ipaddress.ip_address(host).version
    except ValueError:
        return None
    return (host, int(port)) if version == (4 if family == "tcp4" else 6) else None


def nettop_holds(server: Addr, peer: Addr, pid: int, text: str) -> bool:
    """True when ``text``, the output of ``nettop -p <pid> -L 1 -m tcp
    -n -x``, says ``pid`` holds exactly one TCP socket whose local end
    is ``peer`` and far end ``server``, and that socket is Established.

    Read by fixed shape, never by searching: the header must start with
    the known columns, then comes exactly one process row naming
    ``pid``, then socket rows only, every row as wide as the header and
    led by a timestamp. Anything else (a comma or line break in a
    process name, a second process, a new layout, a cut-off listing, no
    listing) is False."""
    if not text.isascii() or not text.endswith("\n"):
        return False
    lines = text[:-1].split("\n")
    if not all(line.isprintable() for line in lines):
        return False
    head = lines[0].split(",")
    if tuple(head[:len(_NETTOP_HEAD)]) != _NETTOP_HEAD:
        return False
    rows = [line.split(",") for line in lines[1:]]
    if not rows or any(len(row) != len(head) or not _NETTOP_TIME.fullmatch(row[0])
                       for row in rows):
        return False
    proc, flows = rows[0], rows[1:]
    named = _NETTOP_PROC.fullmatch(proc[1])
    if (not named or named.group(1) != str(pid) or proc[2] or proc[3]
            or _NETTOP_FLOW.fullmatch(proc[1])):
        return False
    found = []
    for row in flows:
        flow = _NETTOP_FLOW.fullmatch(row[1])
        if not flow:
            return False
        local = _nettop_addr(flow.group(1), flow.group(2))
        remote = _nettop_addr(flow.group(1), flow.group(3))
        if local and remote and _same(local, peer) and _same(remote, server):
            found.append(row)
    # One live connection is one row, as with netstat.
    return len(found) == 1 and found[0][3] == _NETTOP_ESTABLISHED


def _all_pids() -> list[int]:
    """Every pid the kernel lists (``proc_listpids``), or none on
    failure."""
    try:
        import ctypes

        lib = ctypes.CDLL("/usr/lib/libproc.dylib")
        size = lib.proc_listpids(1, 0, None, 0)  # PROC_ALL_PIDS
        if size <= 0:
            return []
        buf = (ctypes.c_int * (size // 4 + 64))()
        size = lib.proc_listpids(1, 0, buf, ctypes.sizeof(buf))
        return [pid for pid in buf[:max(size, 0) // 4] if 0 < pid <= _PID_MAX]
    except (OSError, AttributeError):
        return []


def _pid_start(pid: int) -> tuple[int, int] | None:
    """When ``pid`` started, to the microsecond (``kern.proc.pid``, as
    ``ps`` reads it), or None when there is no such process."""
    import ctypes

    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    libc.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint,
                            ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                            ctypes.c_void_p, ctypes.c_size_t]
    mib = (ctypes.c_int * 4)(1, 14, 1, pid)  # CTL_KERN, KERN_PROC, KERN_PROC_PID
    buf = ctypes.create_string_buffer(_KINFO_PROC_SIZE)
    size = ctypes.c_size_t(len(buf))
    if libc.sysctl(mib, 4, buf, ctypes.byref(size), None, 0) != 0 \
            or size.value != _KINFO_PROC_SIZE \
            or struct.unpack_from("=i", buf, _KINFO_PID_OFFSET)[0] != pid:
        return None
    sec, usec = struct.unpack_from("=qi", buf, 0)
    return sec, usec


Identity = tuple[str, int, tuple[int, int]]


def _identity(pid: int) -> Identity | None:
    """The process at ``pid`` as ``(executable, uid, start time)``, or
    None when any part can't be read or the pid changed hands while it
    was read."""
    start = _pid_start(pid)
    path, uid = _pid_path(pid), _pid_uid(pid)
    if start is None or not path or uid is None or _pid_start(pid) != start:
        return None
    return path, uid, start


def _root_pids(pids: list[int]) -> set[int]:
    """Which of ``pids`` run as root, from one ``ps`` call however many
    there are: anyone can run a file named ``tailscaled``, so the count
    is not the daemon's to choose."""
    if not pids:
        return set()
    out = _run([_PS, "-o", "pid=,uid=", "-p", ",".join(str(pid) for pid in pids)])
    root = set()
    for line in out.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0].isascii() and fields[0].isdigit() \
                and fields[1] == "0":
            root.add(int(fields[0]))
    return root


def _daemon_candidates() -> list[tuple[int, Identity]]:
    """The processes that pass the daemon check ``_darwin_is_proxy``
    applies (root AND ``daemon_executable``), found by executable path
    from the kernel, each with its identity. Never by name."""
    me = os.getpid()
    paths = [pid for pid in _all_pids()
             if pid != me and daemon_executable(_pid_path(pid))]
    found = []
    for pid in sorted(_root_pids(paths) & set(paths)):
        ident = _identity(pid)
        if ident and ident[1] == 0 and daemon_executable(ident[0]):
            found.append((pid, ident))
    return found


def _nettop_pid(server: Addr, peer: Addr,
                candidates: list[tuple[int, Identity]]) -> int | None:
    """The one pid among ``candidates`` that ``nettop`` lists as holding
    the ESTABLISHED socket at ``peer`` (``nettop_holds``), and whose
    identity is the same after the listing as before it; None when no
    candidate or more than one does, or there are too many to ask."""
    if len(candidates) > _MAX_DAEMONS:
        return None
    found = []
    for pid, before in candidates:
        text = _run([_NETTOP, "-p", str(pid), "-L", "1", "-m", "tcp", "-n", "-x"],
                    timeout=_NETTOP_TIMEOUT)
        if nettop_holds(server, peer, pid, text) and _identity(pid) == before:
            found.append(pid)
    return found[0] if len(found) == 1 else None


def _darwin_pid(server: Addr, peer: Addr,
                candidates: Callable[[], list[tuple[int, Identity]]] = _daemon_candidates,
                ) -> int | None:
    """The pid behind ``peer``: from a fresh ``netstat -anv -p tcp``
    when that table can see, else from ``nettop``, asked only about the
    pids ``candidates`` names (the daemon's, for ``is_proxy``)."""
    table = _run([_NETSTAT, "-anv", "-p", "tcp"])
    if netstat_sees(table):
        return netstat_pid(server, peer, table)
    return _nettop_pid(server, peer, candidates())


def _darwin_is_proxy(server: Addr, peer: Addr) -> bool:
    pid = _darwin_pid(server, peer)
    if pid is None or pid < 1 or pid == os.getpid():
        return False
    # Root AND the daemon's executable: either alone is not enough. The
    # same check for a pid from netstat or from nettop.
    if _pid_uid(pid) != 0:
        return False
    return daemon_executable(_pid_path(pid))


def _self_candidate() -> list[tuple[int, Identity]]:
    """This process as the one ``nettop`` candidate, for the probe."""
    ident = _identity(os.getpid())
    return [(os.getpid(), ident)] if ident else []


# -- Can the lookup see anything? ---------------------------------------


def owner_lookup_works(lookup: Callable[[Addr, Addr], int | None] | None = None,
                       me: int | None = None) -> bool:
    """True when this host's socket-owner lookup names this process as
    the owner of a loopback connection it holds itself.

    The lookup ``is_proxy`` relies on (on macOS ``netstat``, or
    ``nettop`` when netstat's table is blind, compared with this pid;
    ``/proc/net/tcp`` on Linux, compared with this uid) is asked about a
    connection opened here for the purpose. False means no source can
    see on this host (see the module docstring) or none can be run:
    ``is_proxy`` then says no to every peer, tailscaled's included.
    Costs one socket pair and one lookup; never raises. Tests pass
    ``lookup`` and ``me``.
    Only a warning and a test skip read the answer.
    """
    try:
        if lookup is None:
            if sys.platform.startswith("linux"):
                lookup, me = linux_uid, os.getuid()
            elif sys.platform == "darwin":
                def lookup(server: Addr, peer: Addr) -> int | None:
                    return _darwin_pid(server, peer, _self_candidate)
                me = os.getpid()
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
