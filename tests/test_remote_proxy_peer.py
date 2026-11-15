"""Only tailscale serve's own connection may name the client (#965).

Behind ``--public-url`` the hub binds loopback, so every request has a
loopback peer. Any local process could dial the same port and write its
own ``X-Forwarded-For`` naming the owner's phone; the hub must believe
the header only when the OS says the peer socket is the Tailscale
daemon's. And this machine's own tailnet node never counts as the
owner's device: any local process reaches the hub as that node through
serve's local listener.
"""

import http.client
import json
import os
import socket
import struct
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from swe.remote import hub as hub_mod
from swe.remote import peer

from tests._tempdir import temp_dir
from tests.test_remote_hub import _Server, _session

_PHONE_IP = "100.64.0.9"
_HUB_IP = "100.64.0.1"
_PEERS = {
    _PHONE_IP: {"node_id": "n-phone", "login": "dev@github",
                "node_name": "iphone.ts.net", "tagged": False},
    _HUB_IP: {"node_id": "n-hub", "login": "dev@github",
              "node_name": "box.ts.net", "tagged": False},
}


class _Owner(unittest.TestCase):
    def setUp(self):
        self.srv = _Server(sessions=[_session()])
        self.addCleanup(self.srv.stop)
        self.srv.state.trust_proxy = True
        self.srv.state.owner_login = "dev@github"
        self.srv.state.own_node_id = "n-hub"
        self.asked: list[str] = []

        def whois(ip):
            self.asked.append(ip)
            return _PEERS.get(ip)

        self.srv.state.whois = whois

    def get(self, ip=_PHONE_IP, path="/v1/sessions"):
        return self.srv.request("GET", path, headers={
            "X-Forwarded-For": ip, "X-Forwarded-Proto": "https",
            "X-Swe-Pair": "1",
        })


class TestForgedForwardedFor(_Owner):
    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0,
                     "as root the test client really is a root peer")
    @unittest.skipUnless(sys.platform == "darwin" or sys.platform.startswith("linux"),
                         "the OS peer check covers macOS and Linux")
    @unittest.skipUnless(
        peer.owner_lookup_works(),
        "this process cannot see socket owners on this host (neither "
        "netstat nor, where its table is blind, nettop: #1944, #1950), so "
        "the real check can't be shown to name this process; it still "
        "fails closed")
    def test_local_process_cannot_claim_the_phone(self):
        # No stub: the real OS check sees this test process, not
        # tailscaled, on the far end of the loopback connection. The
        # socket-owner lookup is wrapped only to record its answer: a
        # lookup that fails also reads as "not the proxy" (fail closed),
        # so a 401 alone would not show the check works on this host.
        if sys.platform == "darwin":
            # netstat, or nettop where netstat's table is blind (#1950).
            # is_proxy asks nettop only about the daemon's pids, so the
            # recording also asks it about this one: the same source must
            # see this process holding the connection.
            lookup, expected = "_darwin_pid", os.getpid()
        else:
            lookup, expected = "linux_uid", os.getuid()
        real = getattr(peer, lookup)
        answers = []

        def recorded(*args, **kwargs):
            if lookup == "_darwin_pid":
                answers.append(real(*args[:2], peer._self_candidate))
                return real(*args, **kwargs)
            answers.append(real(*args, **kwargs))
            return answers[-1]

        with patch.object(peer, lookup, recorded):
            status, _ = self.get()
        self.assertEqual(status, 401)
        # The lookup found the connection and named this process (macOS)
        # or this user (Linux) as its owner.
        self.assertEqual(answers, [expected])
        self.assertNotIn(_PHONE_IP, self.asked)

    def test_non_proxy_peer_reads_as_loopback(self):
        self.srv.state.proxy_peer = lambda server, peer: False
        self.assertEqual(self.get()[0], 401)
        self.assertNotIn(_PHONE_IP, self.asked)

    def test_proxy_peer_names_the_phone(self):
        seen = []

        def is_proxy(server, peer_addr):
            seen.append((server, peer_addr))
            return True

        self.srv.state.proxy_peer = is_proxy
        self.assertEqual(self.get()[0], 200)
        (server, peer_addr), = seen
        self.assertEqual(server, ("127.0.0.1", self.srv.port))
        self.assertEqual(peer_addr[0], "127.0.0.1")

    def test_checked_once_per_connection(self):
        calls = []
        self.srv.state.proxy_peer = lambda s, p: calls.append(p) or True
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=5)
        self.addCleanup(conn.close)
        for _ in range(3):
            conn.request("GET", "/v1/sessions", headers={
                "X-Forwarded-For": _PHONE_IP, "X-Swe-Pair": "1"})
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 200)
        self.assertEqual(len(calls), 1)

    def test_no_forwarded_header_skips_the_check(self):
        calls = []
        self.srv.state.proxy_peer = lambda s, p: calls.append(p) or True
        self.srv.request("GET", "/v1/status")
        self.assertEqual(calls, [])

    def test_a_failing_check_is_not_trust(self):
        def broken(server, peer_addr):
            raise OSError("netstat missing")

        self.srv.state.proxy_peer = broken
        self.assertEqual(self.get()[0], 401)

    def test_pair_request_from_forger_is_not_identified(self):
        self.srv.state.proxy_peer = lambda server, peer: False
        status, data = self.srv.request(
            "POST", "/v1/pair/request", {"name": "x"},
            headers={"X-Forwarded-For": _PHONE_IP})
        self.assertEqual(status, 403, data)
        self.assertEqual(json.loads(data)["error"]["code"], "not_tailnet")


class TestOwnNode(_Owner):
    def setUp(self):
        super().setUp()
        self.srv.state.proxy_peer = lambda server, peer: True

    def test_this_machine_is_not_the_owners_device(self):
        # serve's local listener forwards a local process as this node.
        self.assertEqual(self.get(ip=_HUB_IP)[0], 401)

    def test_owners_other_devices_still_get_in(self):
        self.assertEqual(self.get(ip=_PHONE_IP)[0], 200)

    def test_this_machine_cannot_approve_pair_requests(self):
        status, _ = self.srv.request(
            "POST", "/v1/pair/requests/approve", {"code": "123456"},
            headers={"X-Forwarded-For": _HUB_IP, "X-Swe-Pair": "1",
                     "Content-Type": "application/json"})
        self.assertEqual(status, 401)


class TestConfigureOwnNode(unittest.TestCase):
    def setUp(self):
        self.state = hub_mod.HubState(temp_dir(self))
        self.state.whois = MagicMock(return_value=dict(_PEERS[_HUB_IP]))

    def test_behind_serve(self):
        with patch.object(hub_mod.catalog_mod, "tailscale_ip",
                          return_value=_HUB_IP):
            hub_mod.configure(self.state, "127.0.0.1", 8477,
                              "https://box.ts.net:8477")
        self.assertEqual(self.state.own_node_id, "n-hub")
        self.assertEqual(self.state.owner_login, "dev@github")

    def test_tailnet_bind(self):
        with patch.object(hub_mod.catalog_mod, "tailscale_ip",
                          return_value=_HUB_IP):
            hub_mod.configure(self.state, _HUB_IP, 8477)
        self.assertEqual(self.state.own_node_id, "n-hub")

    def test_tagged_node_still_records_itself(self):
        self.state.whois.return_value = {"node_id": "n-tag", "tagged": True}
        with patch.object(hub_mod.catalog_mod, "tailscale_ip",
                          return_value=_HUB_IP):
            hub_mod.configure(self.state, "127.0.0.1", 8477,
                              "https://box.ts.net:8477")
        self.assertEqual(self.state.own_node_id, "n-tag")
        self.assertEqual(self.state.owner_login, "")


def _hex4(ip: str) -> str:
    """An IPv4 address the way /proc/net/tcp prints it on this host."""
    return "%08X" % struct.unpack("=I", socket.inet_aton(ip))[0]


def _hex6(ip: str) -> str:
    raw = socket.inet_pton(socket.AF_INET6, ip)
    return "".join("%08X" % w for w in struct.unpack("=4I", raw))


_PROC_HEAD = ("  sl  local_address rem_address   st tx_queue rx_queue tr "
              "tm->when retrnsmt   uid  timeout inode\n")


def _proc_line(local, remote, uid, v6=False, st="01"):
    enc = _hex6 if v6 else _hex4
    return (f"   0: {enc(local[0])}:{local[1]:04X} {enc(remote[0])}:{remote[1]:04X} "
            f"{st} 00000000:00000000 00:00000000 00000000 {uid:5d}        0 4242 1\n")


class TestLinux(unittest.TestCase):
    def setUp(self):
        self.root = temp_dir(self)

    def write(self, name, *lines):
        (self.root / name).write_text(_PROC_HEAD + "".join(lines))

    def test_finds_the_peer_sockets_owner(self):
        self.write("tcp",
                   # the hub's own end, then the forger's, then serve's
                   _proc_line(("127.0.0.1", 8477), ("127.0.0.1", 50001), 1000),
                   _proc_line(("127.0.0.1", 50002), ("127.0.0.1", 8477), 1001),
                   _proc_line(("127.0.0.1", 50001), ("127.0.0.1", 8477), 0))
        hub = ("127.0.0.1", 8477)
        self.assertEqual(peer.linux_uid(hub, ("127.0.0.1", 50001), self.root), 0)
        self.assertEqual(peer.linux_uid(hub, ("127.0.0.1", 50002), self.root), 1001)
        self.assertIsNone(peer.linux_uid(hub, ("127.0.0.1", 50003), self.root))

    def test_ipv6_and_mapped_addresses(self):
        self.write("tcp6",
                   _proc_line(("::1", 50005), ("::1", 8477), 0, v6=True),
                   _proc_line(("::ffff:127.0.0.1", 50006),
                              ("::ffff:127.0.0.1", 8477), 0, v6=True))
        self.assertEqual(peer.linux_uid(("::1", 8477), ("::1", 50005), self.root), 0)
        self.assertEqual(peer.linux_uid(("127.0.0.1", 8477),
                                        ("127.0.0.1", 50006), self.root), 0)

    def test_missing_tables(self):
        self.assertIsNone(peer.linux_uid(("127.0.0.1", 1), ("127.0.0.1", 2), self.root))

    def test_is_proxy_only_for_root(self):
        with patch.object(peer.sys, "platform", "linux"), \
                patch.object(peer, "linux_uid", side_effect=[0, 1000, None]):
            args = (("127.0.0.1", 8477), ("127.0.0.1", 50001))
            self.assertTrue(peer.is_proxy(*args))
            self.assertFalse(peer.is_proxy(*args))
            self.assertFalse(peer.is_proxy(*args))


# Columns as macOS 26's ``netstat -anv -p tcp`` prints them.
_NETSTAT = """\
Active Internet connections (including servers)
Proto Recv-Q Send-Q  Local Address          Foreign Address        (state)          rxbytes      txbytes  rhiwat  shiwat          process:pid    state  options           gencnt    flags   flags1 usecnt rtncnt fltrs
tcp4       0      0  127.0.0.1.8477         127.0.0.1.53757        ESTABLISHED         1038          368  407872  146988       python3.13:7001  00102 00000004 0000000000091e65 00000080 01002800      2      0 000000
tcp4       0      0  127.0.0.1.53757        127.0.0.1.8477         ESTABLISHED          840          493  408000  146988 io.tailscale.ipn:1645   00182 00000008 0000000000091e64 00000080 04000900      2      0 000000
tcp4       0      0  127.0.0.1.53760        127.0.0.1.8477         ESTABLISHED          840          493  408000  146988 Google Chrome He:812   00182 00000008 0000000000091e64 00000080 04000900      2      0 000000
tcp4       0      0  127.0.0.1.53761        127.0.0.1.8477         ESTABLISHED          840          493  408000  146988            x:1 y:813   00182 00000008 0000000000091e64 00000080 04000900      2      0 000000
tcp4       0      0  127.0.0.1.53762        127.0.0.1.8477         ESTABLISHED          840          493  408000  146988             z:1:814   00182 00000008 0000000000091e64 00000080 04000900      2      0 000000
tcp6       0      0  ::1.53770              ::1.8477               ESTABLISHED          840          493  408000  146988 io.tailscale.ipn:1645   00182 00000008 0000000000091e64 00000080 04000900      2      0 000000
tcp4       0      0  127.0.0.1.8477         *.*                    LISTEN                 0            0  131072  131072       python3.13:7001  00000 00000006 00000000000872ec 00000000 00002800      1      0 000000
"""


# The executables a macOS host runs Tailscale from, as ``proc_pidpath``
# reports them. Only the daemon's own executable can be the proxy.
_SYSEXT = ("/Library/SystemExtensions/0F3FAF0D-9150-4A9B-9107-01F4D6D0CCD7/"
           "io.tailscale.ipn.macsys.network-extension.systemextension/"
           "Contents/MacOS/io.tailscale.ipn.macsys.network-extension")
_APP = "/Applications/Tailscale.app"
_APPEX = _APP + "/Contents/PlugIns/IPNExtension.appex/Contents/MacOS/IPNExtension"
_GUI = _APP + "/Contents/MacOS/Tailscale"


class TestDarwin(unittest.TestCase):
    hub = ("127.0.0.1", 8477)

    def test_pid_of_the_peer_socket(self):
        self.assertEqual(peer.netstat_pid(self.hub, ("127.0.0.1", 53757), _NETSTAT), 1645)
        self.assertEqual(peer.netstat_pid(self.hub, ("127.0.0.1", 53760), _NETSTAT), 812)
        self.assertEqual(peer.netstat_pid(("::1", 8477), ("::1", 53770), _NETSTAT), 1645)
        # A name is the process's own choice: ``:digits`` inside it must
        # not pass for the pid (``x:1 y`` would read as launchd's pid 1).
        self.assertEqual(peer.netstat_pid(self.hub, ("127.0.0.1", 53761), _NETSTAT), 813)
        self.assertEqual(peer.netstat_pid(self.hub, ("127.0.0.1", 53762), _NETSTAT), 814)
        self.assertIsNone(peer.netstat_pid(self.hub, ("127.0.0.1", 1), _NETSTAT))
        self.assertIsNone(peer.netstat_pid(self.hub, ("127.0.0.1", 53757), ""))

    def _check(self, pid, uid, path=""):
        with patch.object(peer.sys, "platform", "darwin"), \
                patch.object(peer, "_run", return_value=_NETSTAT), \
                patch.object(peer, "_pid_uid", return_value=uid) as get_uid, \
                patch.object(peer, "_pid_path", return_value=path):
            ok = peer.is_proxy(self.hub, ("127.0.0.1", 53757 if pid == 1645 else 53760))
        return ok, get_uid

    def test_root_daemon_is_the_proxy(self):
        ok, get_uid = self._check(1645, 0, _SYSEXT)
        self.assertTrue(ok)
        get_uid.assert_called_once_with(1645)

    def test_root_alone_is_not_the_proxy(self):
        self.assertFalse(self._check(1645, 0, "/sbin/launchd")[0])

    def test_user_process_is_not(self):
        self.assertFalse(self._check(812, 501, "/Applications/Google Chrome.app/x")[0])

    def test_app_store_extension_counts_as_root_when_root_owned(self):
        with patch.object(peer, "app_store_binary", return_value=True) as ok_path:
            self.assertTrue(self._check(1645, 0, _APPEX)[0])
            # Not as another user: no variant's daemon is trusted unless
            # it runs as root.
            self.assertFalse(self._check(1645, 501, _APPEX)[0])
        ok_path.assert_called_once_with(_APPEX)
        with patch.object(peer, "app_store_binary", return_value=False):
            self.assertFalse(self._check(1645, 0, _APPEX)[0])

    def test_app_store_binary_needs_a_root_owned_bundle(self):
        app = temp_dir(self) / "Tailscale.app"
        exe = app / "Contents" / "MacOS" / "Tailscale"
        exe.parent.mkdir(parents=True)
        exe.write_text("")
        # Ownership can't be given away without root: fake what stat
        # reports for the bundle and the binary, (uid, mode) each.
        real_stat = os.stat
        owners = {}

        def stat(path, *args, **kwargs):
            st = real_stat(path, *args, **kwargs)
            if str(path) in owners:
                uid, mode = owners[str(path)]
                return SimpleNamespace(st_uid=uid, st_mode=mode)
            return st

        def check(bundle, binary):
            owners[str(app)] = bundle
            owners[os.path.realpath(exe)] = binary
            with patch.object(peer.os, "stat", side_effect=stat):
                return peer.app_store_binary(str(exe), str(app))

        root_dir, root_exe = (0, 0o40755), (0, 0o100755)
        self.assertTrue(check(root_dir, root_exe))
        self.assertFalse(check((501, 0o40755), root_exe))
        self.assertFalse(check(root_dir, (501, 0o100755)))
        self.assertFalse(check((0, 0o40775), root_exe))  # group-writable
        self.assertFalse(check(root_dir, (0, 0o100757)))  # world-writable
        # Paths outside the bundle never count, whoever owns them.
        owners.clear()
        self.assertFalse(peer.app_store_binary("/tmp/Tailscale", str(app)))
        self.assertFalse(peer.app_store_binary(str(app) + "x/evil", str(app)))


class TestDarwinDaemonOnly(unittest.TestCase):
    """On macOS only the Tailscale daemon's own process is the proxy: root,
    running the daemon's executable. Everything else in the app bundle
    (the app itself, the updater, widgets, the staged copy of the
    extension) is not, whoever runs it."""

    hub = ("127.0.0.1", 8477)

    def decide(self, uid, path, bundle_root_owned=True):
        # Every file and folder reads as root-owned and not writable by
        # anyone else (as on a real install) unless told otherwise, so
        # only the rule under test decides.
        owned = SimpleNamespace(st_uid=0 if bundle_root_owned else 501,
                                st_mode=0o40755)
        with patch.object(peer.sys, "platform", "darwin"), \
                patch.object(peer, "_run", return_value=_NETSTAT), \
                patch.object(peer, "_pid_uid", return_value=uid), \
                patch.object(peer, "_pid_path", return_value=path), \
                patch.object(peer.os, "stat", return_value=owned), \
                patch.object(peer.os.path, "realpath", side_effect=lambda p: p):
            return peer.is_proxy(self.hub, ("127.0.0.1", 53757))

    def test_cli_owned_connection_is_not_the_proxy(self):
        self.assertFalse(self.decide(501, _GUI))

    def test_cli_as_root_is_not_the_proxy_either(self):
        self.assertFalse(self.decide(0, _GUI))

    def test_root_system_extension_is_the_proxy(self):
        self.assertTrue(self.decide(0, _SYSEXT))
        self.assertTrue(self.decide(0, _SYSEXT.replace(
            "0F3FAF0D-9150-4A9B-9107-01F4D6D0CCD7",
            "0f3faf0d-9150-4a9b-9107-01f4d6d0ccd7")))

    def test_system_extension_not_as_root_is_not(self):
        self.assertFalse(self.decide(501, _SYSEXT))

    def test_open_source_tailscaled_as_root_is_the_proxy(self):
        for path in ("/usr/local/bin/tailscaled",
                     "/opt/homebrew/Cellar/tailscale/1.104.5/bin/tailscaled",
                     "/nix/store/0abc-tailscale-1.104.5/bin/tailscaled"):
            with self.subTest(path=path):
                self.assertTrue(self.decide(0, path))

    def test_open_source_tailscaled_as_a_user_is_not(self):
        self.assertFalse(self.decide(501, "/usr/local/bin/tailscaled"))

    def test_other_root_files_in_the_bundle_are_not(self):
        for path in (
                _APP + "/Contents/Frameworks/Sparkle.framework/Versions/B/Autoupdate",
                _APP + "/Contents/PlugIns/IPN-Widgets.appex/Contents/MacOS/IPN-Widgets",
                # The copy the app stages before macOS installs it: the
                # running extension lives under /Library/SystemExtensions.
                _APP + "/Contents/Library/SystemExtensions/"
                "io.tailscale.ipn.macsys.network-extension.systemextension/"
                "Contents/MacOS/io.tailscale.ipn.macsys.network-extension",
                _APP + "/Contents/Resources/tailscaled"):
            for uid in (0, 501):
                with self.subTest(path=path, uid=uid):
                    self.assertFalse(self.decide(uid, path))

    def test_other_root_processes_are_not(self):
        for path in ("/sbin/launchd", "/usr/sbin/sshd", "/usr/bin/nc",
                     "/usr/bin/python3", ""):
            with self.subTest(path=path):
                self.assertFalse(self.decide(0, path))

    def test_lookalike_paths_are_not(self):
        for path in (
                "tailscaled",
                "/usr/local/bin/tailscaled-dev",
                "/usr/local/bin/tailscaled.sh",
                "/tmp/io.tailscale.ipn.macsys.network-extension",
                _SYSEXT.replace("0F3FAF0D-9150-4A9B-9107-01F4D6D0CCD7", "x"),
                _SYSEXT.replace("/Library/", "/Users/me/Library/"),
                _SYSEXT + "/extra",
                "/Library/SystemExtensions/../../tmp/" + _SYSEXT.split("/", 4)[4],
                _APPEX + "2",
                _APPEX.replace("/Applications/", "/Users/me/Applications/")):
            with self.subTest(path=path):
                self.assertFalse(self.decide(0, path))

    def test_app_store_extension_as_root_in_a_root_owned_bundle(self):
        # Not installed on any machine this was checked on: held to the
        # same root rule as the other daemons, at its exact path only.
        self.assertTrue(self.decide(0, _APPEX))
        self.assertFalse(self.decide(501, _APPEX))
        self.assertFalse(self.decide(0, _APPEX, bundle_root_owned=False))

    def test_unknown_owner_is_not(self):
        self.assertFalse(self.decide(None, _SYSEXT))


# One connection's row, in any TCP state. Only an ESTABLISHED row says
# reliably who holds the connection.
_PEER_ROW = ("tcp4       0      0  127.0.0.1.53757        127.0.0.1.8477         "
             "{state:<11}          840          493  408000  146988 {proc:>21}   "
             "00182 00000008 0000000000091e64 00000080 04000900      2      0 000000")


def _row(state="ESTABLISHED", proc="io.tailscale.ipn:1645"):
    return _PEER_ROW.format(state=state, proc=proc) + "\n"


class TestDarwinEstablishedOnly(unittest.TestCase):
    hub = ("127.0.0.1", 8477)
    peer_addr = ("127.0.0.1", 53757)

    def pid(self, text):
        return peer.netstat_pid(self.hub, self.peer_addr, text)

    def test_established_row_names_its_pid(self):
        self.assertEqual(self.pid(_row()), 1645)

    def test_rows_in_other_states_never_match(self):
        for state in ("TIME_WAIT", "FIN_WAIT_1", "FIN_WAIT_2", "CLOSE_WAIT",
                      "CLOSING", "LAST_ACK", "SYN_SENT", "SYN_RECEIVED",
                      "CLOSED", "established", ""):
            with self.subTest(state=state):
                self.assertIsNone(self.pid(_row(state=state)))

    def test_time_wait_row_with_a_stale_pid_is_not_the_proxy(self):
        text = _row(state="TIME_WAIT", proc=":1645")
        with patch.object(peer.sys, "platform", "darwin"), \
                patch.object(peer, "_run", return_value=text), \
                patch.object(peer, "_pid_uid", return_value=0) as get_uid, \
                patch.object(peer, "_pid_path", return_value=_SYSEXT):
            self.assertFalse(peer.is_proxy(self.hub, self.peer_addr))
        get_uid.assert_not_called()

    def test_garbage_pid_text_is_not_a_pid(self):
        for proc in ("io.tailscale.ipn:", "io.tailscale.ipn:abc",
                     "io.tailscale.ipn:16x5", "io.tailscale.ipn:-1645",
                     "io.tailscale.ipn:0", "io.tailscale.ipn:00",
                     "io.tailscale.ipn:99999999999999999999",
                     "io.tailscale.ipn", "-", "?"):
            with self.subTest(proc=proc):
                self.assertIsNone(self.pid(_row(proc=proc)))

    def test_garbage_pid_is_not_the_proxy(self):
        with patch.object(peer.sys, "platform", "darwin"), \
                patch.object(peer, "_run",
                             return_value=_row(proc="kernel_task:0")), \
                patch.object(peer, "_pid_uid", return_value=0), \
                patch.object(peer, "_pid_path", return_value=_SYSEXT):
            self.assertFalse(peer.is_proxy(self.hub, self.peer_addr))

    def test_two_rows_for_one_connection_is_doubt(self):
        # One live connection has one row. Two, whatever their states,
        # means the table can't be read with confidence.
        for other in (_row(), _row(state="TIME_WAIT"),
                      _row(proc="Python:812")):
            with self.subTest(other=other.split()[5]):
                self.assertIsNone(self.pid(_row() + other))
                self.assertIsNone(self.pid(other + _row()))

    def test_row_cut_short_is_doubt(self):
        short = " ".join(_row().split()[:6]) + "\n"
        self.assertIsNone(self.pid(short))
        self.assertIsNone(self.pid(short + _row()))

    def test_other_connections_do_not_count_as_duplicates(self):
        other = _row().replace("127.0.0.1.53757", "127.0.0.1.53758")
        self.assertEqual(self.pid(other + _row() + other), 1645)

    def test_captured_table_still_reads(self):
        # The captured rows are all ESTABLISHED, one per connection.
        self.assertEqual(peer.netstat_pid(self.hub, self.peer_addr, _NETSTAT), 1645)


class TestLinuxEstablishedOnly(unittest.TestCase):
    """Only an ESTABLISHED row says reliably who holds the connection."""

    hub = ("127.0.0.1", 8477)
    peer_addr = ("127.0.0.1", 50001)

    def setUp(self):
        self.root = temp_dir(self)

    def uid(self, *lines):
        (self.root / "tcp").write_text(_PROC_HEAD + "".join(lines))
        return peer.linux_uid(self.hub, self.peer_addr, self.root)

    def test_established_root_row(self):
        self.assertEqual(self.uid(_proc_line(self.peer_addr, self.hub, 0)), 0)

    def test_closing_rows_listed_as_root_never_match(self):
        # 02 SYN_SENT .. 0B CLOSING, and spellings that are not "01".
        for st in ("02", "03", "04", "05", "06", "07", "08", "09", "0A",
                   "0B", "1", "001", "zz"):
            with self.subTest(st=st):
                self.assertIsNone(
                    self.uid(_proc_line(self.peer_addr, self.hub, 0, st=st)))

    def test_closing_row_is_not_the_proxy(self):
        (self.root / "tcp").write_text(
            _PROC_HEAD + _proc_line(self.peer_addr, self.hub, 0, st="05"))
        real = peer.linux_uid
        with patch.object(peer.sys, "platform", "linux"), \
                patch.object(peer, "linux_uid",
                             side_effect=lambda s, p: real(s, p, self.root)):
            self.assertFalse(peer.is_proxy(self.hub, self.peer_addr))

    def test_two_rows_for_one_connection_is_doubt(self):
        live = _proc_line(self.peer_addr, self.hub, 0)
        for other in (live, _proc_line(self.peer_addr, self.hub, 0, st="06"),
                      _proc_line(self.peer_addr, self.hub, 1000)):
            with self.subTest(other=other):
                self.assertIsNone(self.uid(live, other))
                self.assertIsNone(self.uid(other, live))

    def test_row_in_both_tables_is_doubt(self):
        (self.root / "tcp6").write_text(
            _PROC_HEAD + _proc_line(("::ffff:127.0.0.1", 50001),
                                    ("::ffff:127.0.0.1", 8477), 0, v6=True))
        self.assertIsNone(self.uid(_proc_line(self.peer_addr, self.hub, 0)))


class TestDarwinHelpers(unittest.TestCase):
    def test_run_answers_stdout_only_on_success(self):
        ok = MagicMock(returncode=0, stdout="out")
        bad = MagicMock(returncode=1, stdout="partial")
        with patch.object(peer.subprocess, "run", side_effect=[ok, bad]) as run:
            self.assertEqual(peer._run(["x"]), "out")
            self.assertEqual(peer._run(["x"]), "")
        self.assertEqual(run.call_args.kwargs["timeout"], 3)

    def test_pid_uid(self):
        with patch.object(peer, "_run", side_effect=["    0\n", "  501\n", ""]):
            self.assertEqual(peer._pid_uid(1), 0)
            self.assertEqual(peer._pid_uid(2), 501)
            self.assertIsNone(peer._pid_uid(3))

    def test_own_process_is_never_the_proxy(self):
        text = _NETSTAT.replace(":1645 ", f":{os.getpid()} ")
        with patch.object(peer, "_run", return_value=text), \
                patch.object(peer, "_pid_uid", return_value=0) as get_uid:
            self.assertFalse(peer._darwin_is_proxy(("127.0.0.1", 8477), ("127.0.0.1", 53757)))
        get_uid.assert_not_called()

    def test_unlisted_socket_is_not_the_proxy(self):
        with patch.object(peer, "_run", return_value=""):
            self.assertFalse(peer._darwin_is_proxy(("127.0.0.1", 8477), ("127.0.0.1", 53757)))

    @unittest.skipUnless(sys.platform == "darwin", "libproc is macOS's")
    def test_pid_path_reads_the_kernels_path(self):
        # A framework build re-execs into Python.app, so compare with what
        # the kernel holds rather than sys.executable.
        path = peer._pid_path(os.getpid())
        self.assertTrue(path.startswith("/") and os.path.exists(path), path)
        self.assertEqual(peer._pid_path(2 ** 22 + 7), "")


class TestMalformedTables(unittest.TestCase):
    """Rows the parsers can't read are skipped, never trusted."""

    hub = ("127.0.0.1", 8477)

    def test_proc_rows_that_do_not_parse_are_skipped(self):
        root = temp_dir(self)
        good = _proc_line(("127.0.0.1", 50001), self.hub, 0)
        # A row cut short after its addresses has no uid column at all.
        short = " ".join(good.split()[:7]) + "\n"
        (root / "tcp").write_text(
            _PROC_HEAD
            + "   0: short line\n"
            + good.replace(_hex4("127.0.0.1"), "ZZZZZZZZ", 1)
            + good.replace(" 0        0 4242", " root        0 4242"))
        # The bad-hex row is skipped; the matching row's uid column is not
        # a number, so the answer is "can't tell", not uid 0.
        self.assertIsNone(peer.linux_uid(self.hub, ("127.0.0.1", 50001), root))
        (root / "tcp").write_text(
            _PROC_HEAD + "   0: short line\n"
            + good.replace(_hex4("127.0.0.1"), "ZZZZZZZZ", 1) + good)
        self.assertEqual(peer.linux_uid(self.hub, ("127.0.0.1", 50001), root), 0)
        # A row for the same connection cut short is doubt, not skipped:
        # one connection with two rows can't be read with confidence.
        (root / "tcp").write_text(_PROC_HEAD + short + good)
        self.assertIsNone(peer.linux_uid(self.hub, ("127.0.0.1", 50001), root))
        (root / "tcp").write_text(_PROC_HEAD + short)
        self.assertIsNone(peer.linux_uid(self.hub, ("127.0.0.1", 50001), root))

    def test_proc_addr_rejects_bad_fields(self):
        self.assertIsNone(peer._proc_addr("ZZZZZZZZ:1F90"))
        self.assertIsNone(peer._proc_addr("0100007F:XYZ"))
        self.assertIsNone(peer._proc_addr("0100:1F90"))  # neither v4 nor v6
        self.assertEqual(peer._proc_addr("0100007F:1F90"), ("127.0.0.1", 8080))

    def test_names_that_are_not_addresses_compare_verbatim(self):
        self.assertEqual(peer._norm("localhost"), "localhost")
        self.assertEqual(peer._norm("fe80::1%lo0"), "fe80::1")
        self.assertTrue(peer._same(("localhost", 1), ("localhost", 1)))
        self.assertFalse(peer._same(("localhost", 1), ("127.0.0.1", 1)))

    def test_netstat_row_without_a_pid_is_not_a_pid(self):
        line = ("tcp4       0      0  127.0.0.1.53757        127.0.0.1.8477"
                "         ESTABLISHED  840  493  408000  146988  00182\n")
        self.assertIsNone(
            peer.netstat_pid(self.hub, ("127.0.0.1", 53757), line))
        # A bad address column is skipped like any other row.
        bad = line.replace("127.0.0.1.53757", "127.0.0.1.port")
        self.assertIsNone(peer.netstat_pid(self.hub, ("127.0.0.1", 53757), bad))

    def test_app_store_binary_refuses_links_out_and_missing_files(self):
        base = temp_dir(self)
        app = base / "Tailscale.app"
        (app / "Contents").mkdir(parents=True)
        outside = base / "evil"
        outside.write_text("")
        link = app / "Contents" / "link"
        link.symlink_to(outside)
        # The path names the bundle, but the file it resolves to is not
        # inside it: refused even when stat reports everything root-owned.
        root_owned = SimpleNamespace(st_uid=0, st_mode=0o40755)
        with patch.object(peer.os, "stat", return_value=root_owned):
            self.assertFalse(peer.app_store_binary(str(link), str(app)))
        # Inside the bundle but gone: stat fails, so no.
        self.assertFalse(
            peer.app_store_binary(str(app / "Contents" / "gone"), str(app)))


class TestOwnerLookupWorks(unittest.TestCase):
    """Whether this host's socket-owner lookup can see anything (#1944).
    It only feeds a warning and a test skip: is_proxy never reads it."""

    def test_lookup_that_names_this_process_works(self):
        seen = []

        def lookup(server, peer_addr):
            seen.append((server, peer_addr))
            return 42

        self.assertTrue(peer.owner_lookup_works(lookup, 42))
        # Asked about a real loopback connection: the listener's address,
        # then the client end this process holds.
        (server, peer_addr), = seen
        self.assertEqual(server[0], "127.0.0.1")
        self.assertEqual(peer_addr[0], "127.0.0.1")
        self.assertNotEqual(server[1], peer_addr[1])

    def test_connection_is_open_while_asked(self):
        def lookup(server, peer_addr):
            # A second dial proves the listener is still up; the probe's
            # own client socket is the one whose owner is asked for.
            with socket.create_connection(server, timeout=2):
                pass
            return 7

        self.assertTrue(peer.owner_lookup_works(lookup, 7))

    def test_blind_lookup_does_not_work(self):
        self.assertFalse(peer.owner_lookup_works(lambda s, p: None, 42))

    def test_someone_else_named_does_not_work(self):
        self.assertFalse(peer.owner_lookup_works(lambda s, p: 1, 42))

    def test_errors_do_not_work_and_never_raise(self):
        def boom(server, peer_addr):
            raise OSError("netstat missing")

        self.assertFalse(peer.owner_lookup_works(boom, 42))
        with patch.object(peer.socket, "create_server",
                          side_effect=OSError("no loopback")):
            self.assertFalse(peer.owner_lookup_works(lambda s, p: 42, 42))

    def test_darwin_asks_netstat_for_this_pid(self):
        # The same lookup is_proxy uses on macOS: netstat_pid over
        # ``netstat -anv -p tcp`` while that table can see, compared
        # with this process's pid. nettop is not asked.
        with patch.object(peer.sys, "platform", "darwin"), \
                patch.object(peer, "_run", return_value=_NETSTAT) as run, \
                patch.object(peer, "nettop_holds") as holds, \
                patch.object(peer, "netstat_pid",
                             side_effect=[os.getpid(), None]):
            self.assertTrue(peer.owner_lookup_works())
            self.assertFalse(peer.owner_lookup_works())
        run.assert_called_with([peer._NETSTAT, "-anv", "-p", "tcp"])
        holds.assert_not_called()

    def test_darwin_blind_netstat_asks_nettop_about_this_pid(self):
        # netstat's table is empty (#1950): the probe asks nettop about
        # this process, as is_proxy asks it about the daemon's.
        me = os.getpid()
        ident = ("/usr/bin/python3", os.getuid(), (1, 0))
        calls = []

        def run(argv, timeout=3):
            calls.append(argv)
            return "" if argv[0] == peer._NETSTAT else "LISTING"

        for answer in (True, False):
            with self.subTest(answer=answer), \
                    patch.object(peer.sys, "platform", "darwin"), \
                    patch.object(peer, "_run", side_effect=run), \
                    patch.object(peer, "_identity", return_value=ident), \
                    patch.object(peer, "_daemon_candidates") as daemons, \
                    patch.object(peer, "nettop_holds", return_value=answer) as holds:
                self.assertIs(peer.owner_lookup_works(), answer)
                (server, client, pid, text), _ = holds.call_args
                self.assertEqual((pid, text), (me, "LISTING"))
                self.assertEqual(server[0], "127.0.0.1")
                self.assertNotEqual(server, client)
                daemons.assert_not_called()
        self.assertIn([peer._NETTOP, "-p", str(me), "-L", "1", "-m", "tcp", "-n", "-x"],
                      calls)

    def test_darwin_blind_everywhere_does_not_work(self):
        # netstat empty and nettop gone: the warning's case.
        def run(argv, timeout=3):
            if argv[0] == peer._NETTOP:
                raise FileNotFoundError(argv[0])
            return ""

        with patch.object(peer.sys, "platform", "darwin"), \
                patch.object(peer, "_run", side_effect=run), \
                patch.object(peer, "_identity", return_value=("/x", 501, (1, 0))):
            self.assertFalse(peer.owner_lookup_works())

    def test_linux_asks_proc_for_this_uid(self):
        with patch.object(peer.sys, "platform", "linux"), \
                patch.object(peer, "linux_uid",
                             side_effect=[os.getuid(), None]):
            self.assertTrue(peer.owner_lookup_works())
            self.assertFalse(peer.owner_lookup_works())

    def test_other_platforms_have_no_lookup(self):
        with patch.object(peer.sys, "platform", "win32"), \
                patch.object(peer, "linux_uid") as uid, \
                patch.object(peer, "netstat_pid") as pid:
            self.assertFalse(peer.owner_lookup_works())
        uid.assert_not_called()
        pid.assert_not_called()

    @unittest.skipUnless(sys.platform.startswith("linux"),
                         "/proc/net/tcp is Linux's")
    def test_linux_sees_its_own_socket(self):
        # /proc/net/tcp is world-readable, so on Linux (CI) the detector
        # must say yes: a detector broken to "no" would otherwise skip
        # the real-check test everywhere without anyone noticing.
        self.assertTrue(peer.owner_lookup_works())


class TestBlindLookupWarning(unittest.TestCase):
    """``swe remote serve`` warns once when it trusts tailscale serve's
    headers but cannot see who sent them (#1944)."""

    def setUp(self):
        self.state = hub_mod.HubState(temp_dir(self))
        self.state.whois = MagicMock(return_value=dict(_PEERS[_HUB_IP]))
        self.asked = 0

    def works(self, answer):
        def probe():
            self.asked += 1
            return answer
        return probe

    def warning(self, platform="darwin", answer=False):
        self.state.proxy_lookup_works = self.works(answer)
        with patch.object(hub_mod.sys, "platform", platform):
            return hub_mod.proxy_lookup_warning(self.state)

    def test_blind_lookup_behind_serve_warns(self):
        self.state.trust_proxy = True
        text = self.warning()
        self.assertIn("tailscale serve", text)
        self.assertIn("tailnet identity", text)
        self.assertIn("Developer-ID", text)
        self.assertNotIn("\u2014", text)
        self.assertEqual(self.asked, 1)

    def test_linux_names_its_own_fix(self):
        self.state.trust_proxy = True
        text = self.warning("linux")
        self.assertIn("/proc/net/tcp", text)
        self.assertNotIn("Developer-ID", text)

    def test_working_lookup_is_quiet(self):
        self.state.trust_proxy = True
        self.assertEqual(self.warning(answer=True), "")

    def test_without_proxy_trust_nothing_is_probed(self):
        # No --public-url (or a tailnet-IP bind): the check never runs.
        self.assertFalse(self.state.trust_proxy)
        self.assertEqual(self.warning(), "")
        self.assertEqual(self.asked, 0)

    def test_platforms_without_the_check_are_quiet(self):
        self.state.trust_proxy = True
        self.assertEqual(self.warning("win32"), "")
        self.assertEqual(self.asked, 0)

    def test_a_failing_probe_reads_as_blind(self):
        self.state.trust_proxy = True

        def broken():
            raise OSError("boom")

        self.state.proxy_lookup_works = broken
        with patch.object(hub_mod.sys, "platform", "darwin"):
            self.assertIn("Developer-ID",
                          hub_mod.proxy_lookup_warning(self.state))

    def test_warning_never_changes_what_the_check_accepts(self):
        self.state.trust_proxy = True
        before = self.state.proxy_peer
        self.warning()
        self.assertIs(self.state.proxy_peer, before)
        self.assertIs(self.state.proxy_peer, peer.is_proxy)
        self.assertTrue(self.state.trust_proxy)

    def serve(self, url, answer):
        import io

        server = MagicMock()
        server.serve_forever.side_effect = KeyboardInterrupt
        self.state.start = MagicMock()
        self.state.stop = MagicMock()
        self.state.proxy_lookup_works = self.works(answer)
        err = io.StringIO()
        with patch.object(hub_mod, "make_server", return_value=server), \
                patch.object(hub_mod.sys, "platform", "darwin"), \
                patch.object(hub_mod.catalog_mod, "tailscale_ip",
                             return_value=_HUB_IP), \
                patch.object(hub_mod.sys, "stderr", err):
            hub_mod.serve(self.state, "127.0.0.1", 8477, url)
        return err.getvalue()

    def test_serve_behind_tailscale_serve_warns_once(self):
        out = self.serve("https://box.ts.net:8477", answer=False)
        self.assertEqual(out.count("Developer-ID"), 1, out)
        self.assertIn("warning:", out)
        self.assertEqual(self.asked, 1)

    def test_serve_without_public_url_does_not_probe(self):
        out = self.serve("", answer=False)
        self.assertNotIn("Developer-ID", out)
        self.assertEqual(self.asked, 0)

    def test_serve_with_a_working_lookup_is_quiet(self):
        out = self.serve("https://box.ts.net:8477", answer=True)
        self.assertNotIn("warning:", out)
        self.assertEqual(self.asked, 1)


# ``nettop -p 1875 -L 1 -m tcp -n -x`` as macOS 27 prints it for the
# Standalone daemon, captured from a blind (development-signed) Python
# and scrubbed: outside and tailnet addresses are documentation ones.
# The two rows to 127.0.0.1:8477 are serve's connections to the hub; the
# rest are the daemon's other sockets, left in for shape.
_NETTOP_HEAD = ("time,,interface,state,bytes_in,bytes_out,rx_dupe,rx_ooo,re-tx,"
                "rtt_avg,rcvsize,tx_win,tc_class,tc_mgt,cc_algo,P,C,R,W,arch,")
_NETTOP_TAIL = ",0,0,0,0,0,1.00 ms,408300,408320,BE,-,cubic,-,-,-,-,so,"
_NETTOP = _NETTOP_HEAD + """
11:37:59.281976,io.tailscale.ip.1875,,,23062377,21494933,145299,0,115264,,,,,,,,,,,,
11:37:59.281951,tcp4 192.0.2.13:51470<->198.51.100.233:443,en0,SynSent,0,0,0,0,0,0.00 ms,131072,0,BE,-,cubic,-,-,-,-,so,
11:37:59.281885,tcp4 192.0.2.13:51465<->198.51.100.36:443,en0,FinWait1,5355,1731,0,0,0,387.47 ms,131072,64128,BE,-,cubic,-,-,-,-,so,
11:37:59.261967,tcp4 127.0.0.1:49206<->*:*,lo0,Listen,,,,,,,,,,-,cubic,-,-,-,-,so,
11:37:59.262083,tcp6 *.443<->*.*,,Listen,,,,,,,,,,-,cubic,-,-,-,-,so,
11:37:59.262207,tcp4 100.64.0.1:8477<->*:*,utun4,Listen,,,,,,,,,,-,cubic,-,-,-,-,so,
11:37:59.262282,tcp6 fd7a:115c:a1e0::1.8477<->*.*,utun4,Listen,,,,,,,,,,-,cubic,-,-,-,-,so,
11:37:59.263103,tcp4 127.0.0.1:49206<->127.0.0.1:49721,lo0,Established,327,19411497,0,0,47128,1.22 ms,408000,502016,BE,-,cubic,-,-,-,-,so,
11:37:59.263300,tcp4 127.0.0.1:53757<->127.0.0.1:8477,lo0,Established,840,493,0,0,0,1.00 ms,408300,408320,BE,-,cubic,-,-,-,-,so,
11:37:59.263400,tcp4 127.0.0.1:53758<->127.0.0.1:8477,lo0,Established,840,493,0,0,0,1.00 ms,408300,408320,BE,-,cubic,-,-,-,-,so,
11:37:59.263500,tcp6 ::1.53770<->::1.8477,lo0,Established,840,493,0,0,0,1.00 ms,407800,407808,BE,-,cubic,-,-,-,-,so,
11:37:59.265746,tcp4 192.0.2.13:50952<->203.0.113.8:443,en0,Established,22940109,1666558,142065,0,55987,53.75 ms,458320,335616,BE,-,cubic,-,-,-,-,so,
"""
# What nettop prints for a pid that does not exist, or has no TCP
# sockets: the header alone, exit status 0.
_NETTOP_NOBODY = _NETTOP_HEAD + "\n"


def _listing(pid=1875, name="io.tailscale.ip", rows=()):
    """A nettop listing for ``pid``: its process row, then ``rows``,
    each ``(flow, state)``."""
    lines = [_NETTOP_HEAD,
             f"11:40:36.050938,{name}.{pid},,,0,0,0,0,0,,,,,,,,,,,,"]
    lines += [f"11:40:36.050911,{flow},lo0,{state}{_NETTOP_TAIL}"
              for flow, state in rows]
    return "\n".join(lines) + "\n"


_SERVE = "tcp4 127.0.0.1:53757<->127.0.0.1:8477"


class TestNettopListing(unittest.TestCase):
    """``nettop_holds`` reads a listing by its fixed shape only."""

    hub = ("127.0.0.1", 8477)
    peer_addr = ("127.0.0.1", 53757)

    def holds(self, text, pid=1875, peer_addr=None):
        return peer.nettop_holds(self.hub, peer_addr or self.peer_addr, pid, text)

    def test_captured_listing_names_serves_connections(self):
        self.assertTrue(self.holds(_NETTOP))
        self.assertTrue(self.holds(_NETTOP, peer_addr=("127.0.0.1", 53758)))
        self.assertTrue(peer.nettop_holds(("::1", 8477), ("::1", 53770), 1875, _NETTOP))
        # A dual-stack socket's v4-mapped spelling is the same connection.
        self.assertTrue(self.holds(_NETTOP, peer_addr=("::ffff:127.0.0.1", 53757)))

    def test_other_connections_are_not_this_one(self):
        for addr in (("127.0.0.1", 53759), ("127.0.0.1", 49721),
                     ("127.0.0.1", 49206), ("127.0.0.2", 53757)):
            with self.subTest(addr=addr):
                self.assertFalse(self.holds(_NETTOP, peer_addr=addr))
        # Ends the other way round: the hub's own end of the connection.
        self.assertFalse(peer.nettop_holds(self.peer_addr, self.hub, 1875, _NETTOP))
        # The same pair against another hub port.
        self.assertFalse(peer.nettop_holds(("127.0.0.1", 8478), self.peer_addr,
                                           1875, _NETTOP))

    def test_listing_for_another_pid_is_not_this_pids(self):
        self.assertFalse(self.holds(_NETTOP, pid=1876))
        self.assertFalse(self.holds(_NETTOP, pid=187))

    def test_only_established_counts(self):
        for state in ("SynSent", "SynReceived", "FinWait1", "FinWait2",
                      "CloseWait", "Closing", "LastAck", "TimeWait",
                      "Closed", "Listen", "established", "ESTABLISHED", ""):
            with self.subTest(state=state):
                self.assertFalse(self.holds(_listing(rows=[(_SERVE, state)])))
        self.assertTrue(self.holds(_listing(rows=[(_SERVE, "Established")])))

    def test_two_rows_for_one_connection_is_doubt(self):
        for other in ("Established", "TimeWait", "FinWait2"):
            with self.subTest(other=other):
                self.assertFalse(self.holds(_listing(
                    rows=[(_SERVE, "Established"), (_SERVE, other)])))
                self.assertFalse(self.holds(_listing(
                    rows=[(_SERVE, other), (_SERVE, "Established")])))

    def test_empty_listing_is_doubt(self):
        # A pid that does not exist, or holds no TCP socket.
        self.assertFalse(self.holds(_NETTOP_NOBODY))
        self.assertFalse(self.holds(""))
        self.assertFalse(self.holds(_listing(rows=[])))

    def test_changed_layout_is_doubt(self):
        good = _listing(rows=[(_SERVE, "Established")])
        for bad in (
                good.replace("time,,interface,state,", "time,,state,interface,", 1),
                good.replace("time,,interface,state,", "time,interface,state,", 1),
                good.replace(",arch,\n", ",arch,extra,\n", 1),  # header wider
                good.replace(",so,\n", ",so,x,\n"),             # rows wider
                good.replace("11:40:36.050911,", "", 1),        # no time
                good.replace("11:40:36.050911", "yesterday", 1),
                good.rstrip("\n"),                               # cut off
                good.replace(",lo0,", ",lo0\r,", 1),
                good.replace("io.tailscale.ip", "io.tailscale.ïp", 1),
                good.replace(_SERVE, "udp4 127.0.0.1:53757<->127.0.0.1:8477"),
                good.replace(_SERVE, _SERVE.replace("<->", " <-> "))):
            with self.subTest(bad=bad[-120:]):
                self.assertFalse(self.holds(bad))

    def test_process_row_must_come_first_and_alone(self):
        row = f"11:40:36.050911,{_SERVE},lo0,Established{_NETTOP_TAIL}"
        proc = "11:40:36.050938,io.tailscale.ip.1875,,,0,0,0,0,0,,,,,,,,,,,,"
        other = "11:40:36.050938,Python.812,,,0,0,0,0,0,,,,,,,,,,,,"
        for lines in ([row, proc], [row], [proc, row, other], [proc, other, row],
                      [proc, proc, row]):
            with self.subTest(lines=lines):
                self.assertFalse(self.holds("\n".join([_NETTOP_HEAD, *lines]) + "\n"))
        # A v6 socket row ends in ``.<digits>`` too; it is never the
        # process row, even with its interface and state emptied.
        v6 = "11:40:36.050938,tcp6 ::1.53770<->::1.1875,,,0,0,0,0,0,,,,,,,,,,,,"
        self.assertFalse(self.holds("\n".join([_NETTOP_HEAD, v6, row]) + "\n"))
        # A process row carries no interface or state.
        self.assertFalse(self.holds("\n".join(
            [_NETTOP_HEAD, proc.replace(",,,0,", ",lo0,Established,0,", 1), row]) + "\n"))

    def test_names_that_break_the_shape_are_doubt(self):
        # A name is the process's own choice. nettop prints it raw:
        # commas, quotes and line breaks included (checked on macOS 27).
        rows = [(_SERVE, "Established")]
        for name in ("x,y", "a,,b", "nl\nx", "nl\n11:40:36.1",
                     f"x\n11:40:36.1,{_SERVE},lo0,Established{_NETTOP_TAIL}\ny"):
            with self.subTest(name=name):
                self.assertFalse(self.holds(_listing(name=name, rows=rows)))

    def test_names_that_look_like_pids_are_not_pids(self):
        rows = [(_SERVE, "Established")]
        # ``evil.1875`` run as pid 65045 prints as ``evil.1875.65045``.
        text = _listing(pid=65045, name="evil.1875", rows=rows)
        self.assertFalse(self.holds(text, pid=1875))
        self.assertTrue(self.holds(text, pid=65045))
        self.assertFalse(self.holds(_listing(pid=65041, name="1875", rows=rows)))
        # Quotes and the daemon's own name are just names.
        self.assertTrue(self.holds(_listing(name='q"u', rows=rows)))
        self.assertFalse(self.holds(_listing(pid="1875x", rows=rows)))
        self.assertFalse(self.holds(_listing(pid="01875", rows=rows), pid=1875))

    def test_addresses_must_fit_their_family(self):
        for flow in ("tcp4 ::1.53757<->::1.8477",
                     "tcp6 127.0.0.1:53757<->127.0.0.1:8477",
                     "tcp4 127.0.0.1.53757<->127.0.0.1.8477",
                     "tcp4 127.0.0.1:53757<->127.0.0.1:08477x",
                     "tcp4 127.0.0.1:0<->127.0.0.1:8477",
                     "tcp4 127.0.0.1:99999<->127.0.0.1:8477",
                     "tcp4 localhost:53757<->localhost:8477"):
            with self.subTest(flow=flow):
                self.assertFalse(self.holds(_listing(rows=[(flow, "Established")])))

    @unittest.skipUnless(sys.platform == "darwin" and os.path.exists(peer._NETTOP),
                         "nettop is macOS's")
    def test_reads_this_hosts_real_nettop(self):
        # The real tool's shape on this host, about a connection this
        # process holds both ends of.
        with socket.create_server(("127.0.0.1", 0)) as listener:
            server = listener.getsockname()[:2]
            with socket.create_connection(server, timeout=3) as client:
                conn, _ = listener.accept()
                with conn:
                    text = peer._run([peer._NETTOP, "-p", str(os.getpid()), "-L", "1",
                                      "-m", "tcp", "-n", "-x"])
                    mine = client.getsockname()[:2]
                    self.assertTrue(peer.nettop_holds(server, mine, os.getpid(), text),
                                    text)
                    self.assertFalse(peer.nettop_holds(server, mine, os.getpid() + 1,
                                                       text))
                    self.assertFalse(peer.nettop_holds(mine, server, 1, text))


_TS_CLI = _APP + "/Contents/MacOS/tailscale"


class _BlindHost:
    """A macOS host whose netstat table is blind (#1950) unless given
    one: every process lookup ``peer`` makes is answered from here."""

    def __init__(self, test, procs, listings, table=""):
        # pid -> [path, uid, start]; mutable to stage pid reuse.
        self.procs = {pid: list(v) for pid, v in procs.items()}
        self.listings = listings
        self.table = table
        self.asked: list[int] = []
        self.ps_calls = 0
        self.on_nettop = None
        self.test = test

    def run(self, argv, timeout=3):
        if argv[0] == peer._NETSTAT:
            return self.table
        if argv[0] == peer._PS:
            # ``ps -o pid=,uid= -p 1,2,...``, one line per live pid.
            self.test.assertEqual(argv[1:4], ["-o", "pid=,uid=", "-p"])
            self.ps_calls += 1
            rows = []
            for pid in map(int, argv[4].split(",")):
                uid = self.get(pid, 1)
                if uid is not None:
                    rows.append(f"{pid:5d} {uid:5d}")
            return "\n".join(rows) + "\n"
        if argv[0] == peer._NETTOP:
            self.test.assertEqual(timeout, peer._NETTOP_TIMEOUT)
            self.test.assertEqual(argv[1], "-p")
            pid = int(argv[2])
            self.asked.append(pid)
            if self.on_nettop:
                self.on_nettop(pid)
            listing = self.listings.get(pid, _NETTOP_NOBODY)
            if isinstance(listing, BaseException):
                raise listing
            return listing
        raise AssertionError(argv)

    def get(self, pid, i):
        proc = self.procs.get(pid)
        return proc[i] if proc else None

    def is_proxy(self, peer_addr=("127.0.0.1", 53757), server=("127.0.0.1", 8477)):
        with patch.object(peer.sys, "platform", "darwin"), \
                patch.object(peer, "_run", side_effect=self.run), \
                patch.object(peer, "_all_pids", side_effect=lambda: list(self.procs)), \
                patch.object(peer, "_pid_path", side_effect=lambda p: self.get(p, 0) or ""), \
                patch.object(peer, "_pid_uid", side_effect=lambda p: self.get(p, 1)), \
                patch.object(peer, "_pid_start", side_effect=lambda p: self.get(p, 2)):
            return peer.is_proxy(server, peer_addr)


_DAEMON = {1875: (_SYSEXT, 0, (1794358937, 617876))}


class TestBlindNetstatNettop(unittest.TestCase):
    """When netstat's table is blind, nettop answers, under the same
    rule: root, the daemon's own executable, exactly this connection,
    Established (#1950)."""

    def host(self, procs=None, listings=None, table=""):
        procs = dict(_DAEMON) if procs is None else procs
        listings = {1875: _NETTOP} if listings is None else listings
        return _BlindHost(self, procs, listings, table)

    def test_blind_netstat_nettop_sees_the_daemon(self):
        host = self.host()
        self.assertTrue(host.is_proxy())
        self.assertTrue(host.is_proxy(("127.0.0.1", 53758)))
        self.assertEqual(host.asked, [1875, 1875])

    def test_a_table_netstat_can_see_is_not_second_guessed(self):
        # netstat sees, and names a user process: that is the answer.
        host = self.host(procs={**_DAEMON, 812: ("/usr/bin/nc", 501, (1, 0))},
                         table=_NETSTAT)
        self.assertFalse(host.is_proxy(("127.0.0.1", 53760)))
        # netstat sees, and names the daemon: also the answer.
        host.procs[1645] = [_SYSEXT, 0, (1, 0)]
        self.assertTrue(host.is_proxy(("127.0.0.1", 53757)))
        self.assertEqual(host.asked, [])

    def test_direct_local_connection_is_refused(self):
        # A local process dialling the hub holds its own end: its row is
        # under its own pid, which is never asked about.
        local = 4242
        host = self.host(
            procs={**_DAEMON, local: ("/usr/bin/python3", 501, (5, 0))},
            listings={1875: _NETTOP, local: _listing(
                pid=local, name="python3",
                rows=[("tcp4 127.0.0.1:53759<->127.0.0.1:8477", "Established")])})
        self.assertFalse(host.is_proxy(("127.0.0.1", 53759)))
        self.assertEqual(host.asked, [1875])

    def test_cli_relay_is_refused(self):
        # ``tailscale nc`` holds the connection in the CLI process, which
        # runs from the app bundle but is not the daemon, root or not.
        for uid in (501, 0):
            with self.subTest(uid=uid):
                cli = 65194
                host = self.host(
                    procs={**_DAEMON, cli: (_TS_CLI, uid, (6, 0))},
                    listings={1875: _NETTOP, cli: _listing(
                        pid=cli, name="tailscale",
                        rows=[("tcp4 127.0.0.1:54551<->127.0.0.1:8477",
                               "Established")])})
                self.assertFalse(host.is_proxy(("127.0.0.1", 54551)))
                self.assertEqual(host.asked, [1875])

    def test_names_never_pick_a_process(self):
        # Root processes named like the daemon, or like its pid, whose
        # executables are not the daemon's: never asked.
        flow = [("tcp4 127.0.0.1:53759<->127.0.0.1:8477", "Established")]
        fakes = {6001: ("/tmp/io.tailscale.ipn.macsys.network-extension", 0, (7, 0)),
                 6002: ("/tmp/1875", 0, (7, 1)),
                 6003: ("/usr/local/bin/tailscaled-dev", 0, (7, 2))}
        host = self.host(procs={**_DAEMON, **fakes},
                         listings={1875: _NETTOP,
                                   **{p: _listing(pid=p, rows=flow) for p in fakes}})
        self.assertFalse(host.is_proxy(("127.0.0.1", 53759)))
        self.assertEqual(host.asked, [1875])

    def test_many_user_processes_named_tailscaled_cost_one_ps(self):
        # Anyone can run a file named tailscaled; as a user it is never a
        # candidate, and a crowd of them costs one ps call, not one each.
        crowd = {pid: (f"/tmp/x{pid}/tailscaled", 501, (pid, 0))
                 for pid in range(2000, 2200)}
        host = self.host(procs={**_DAEMON, **crowd})
        self.assertTrue(host.is_proxy())
        self.assertEqual(host.asked, [1875])
        self.assertEqual(host.ps_calls, 1)

    def test_root_pids_reads_only_well_formed_root_rows(self):
        out = "    1     0\n   77   501\n 1875     0\n  abc     0\n 99 0 x\n\n"
        with patch.object(peer, "_run", return_value=out) as run:
            self.assertEqual(peer._root_pids([1, 77, 1875]), {1, 1875})
        run.assert_called_once_with([peer._PS, "-o", "pid=,uid=", "-p", "1,77,1875"])
        with patch.object(peer, "_run") as run:
            self.assertEqual(peer._root_pids([]), set())
        run.assert_not_called()

    def test_daemon_executable_not_as_root_is_never_asked(self):
        host = self.host(procs={1875: (_SYSEXT, 501, (1, 0))})
        self.assertFalse(host.is_proxy())
        self.assertEqual(host.asked, [])

    def test_open_source_tailscaled_as_root(self):
        host = self.host(procs={77: ("/usr/local/bin/tailscaled", 0, (1, 0))},
                         listings={77: _listing(pid=77, name="tailscaled",
                                                rows=[(_SERVE, "Established")])})
        self.assertTrue(host.is_proxy())

    def test_another_daemon_connection_is_not_this_one(self):
        # serve holds 53757 and 53758; a third port is someone else's.
        host = self.host()
        self.assertFalse(host.is_proxy(("127.0.0.1", 53759)))
        self.assertTrue(host.is_proxy(("127.0.0.1", 53757)))

    def test_stale_rows_are_refused(self):
        for state in ("TimeWait", "FinWait2", "CloseWait", "Closed"):
            with self.subTest(state=state):
                host = self.host(listings={1875: _listing(rows=[(_SERVE, state)])})
                self.assertFalse(host.is_proxy())

    def test_pid_reused_during_the_listing_is_refused(self):
        # The daemon exits and its pid goes to another process while
        # nettop runs: path, uid or start time no longer match.
        for change in ((_SYSEXT, 0, (1794358999, 0)),         # restarted
                       ("/usr/bin/python3", 501, (1794358999, 0)),
                       ("/usr/bin/python3", 0, (1794358937, 617876)),
                       (_SYSEXT, 501, (1794358937, 617876)),
                       None):                                  # gone
            with self.subTest(change=change):
                host = self.host()

                def reuse(pid, host=host, change=change):
                    if change is None:
                        del host.procs[pid]
                    else:
                        host.procs[pid] = list(change)

                host.on_nettop = reuse
                self.assertFalse(host.is_proxy())

    def test_pid_changing_hands_while_identified_is_refused(self):
        # Start time read before and after the path and uid: a pid that
        # changed hands in between has no identity.
        starts = iter([(1, 0), (2, 0)])
        with patch.object(peer, "_pid_path", return_value=_SYSEXT), \
                patch.object(peer, "_pid_uid", return_value=0), \
                patch.object(peer, "_pid_start", side_effect=lambda p: next(starts)):
            self.assertIsNone(peer._identity(1875))
        with patch.object(peer, "_pid_path", return_value=_SYSEXT), \
                patch.object(peer, "_pid_uid", return_value=0), \
                patch.object(peer, "_pid_start", return_value=(1, 0)):
            self.assertEqual(peer._identity(1875), (_SYSEXT, 0, (1, 0)))
        for path, uid, start in (("", 0, (1, 0)), (_SYSEXT, None, (1, 0)),
                                 (_SYSEXT, 0, None)):
            with self.subTest(path=path, uid=uid, start=start), \
                    patch.object(peer, "_pid_path", return_value=path), \
                    patch.object(peer, "_pid_uid", return_value=uid), \
                    patch.object(peer, "_pid_start", return_value=start):
                self.assertIsNone(peer._identity(1875))

    def test_two_daemons_claiming_one_connection_is_doubt(self):
        procs = {**_DAEMON, 77: ("/usr/local/bin/tailscaled", 0, (1, 0))}
        host = self.host(procs=procs, listings={
            1875: _NETTOP, 77: _listing(pid=77, rows=[(_SERVE, "Established")])})
        self.assertFalse(host.is_proxy())
        self.assertEqual(sorted(host.asked), [77, 1875])

    def test_too_many_daemons_is_doubt_without_asking(self):
        procs = {pid: ("/usr/local/bin/tailscaled", 0, (pid, 0))
                 for pid in range(100, 100 + peer._MAX_DAEMONS + 1)}
        host = self.host(procs=procs,
                         listings={pid: _listing(pid=pid, rows=[(_SERVE, "Established")])
                                   for pid in procs})
        self.assertFalse(host.is_proxy())
        self.assertEqual(host.asked, [])

    def test_this_process_is_never_a_daemon_candidate(self):
        me = os.getpid()
        host = self.host(procs={me: (_SYSEXT, 0, (1, 0))},
                         listings={me: _listing(pid=me, rows=[(_SERVE, "Established")])})
        self.assertFalse(host.is_proxy())
        self.assertEqual(host.asked, [])

    def test_nettop_failing_is_not_trust(self):
        for listing in (FileNotFoundError(peer._NETTOP),
                        peer.subprocess.TimeoutExpired(peer._NETTOP, 2),
                        "", _NETTOP_NOBODY, _NETTOP.replace("state", "status", 1)):
            with self.subTest(listing=repr(listing)[:40]):
                self.assertFalse(self.host(listings={1875: listing}).is_proxy())

    def test_no_daemon_running(self):
        self.assertFalse(self.host(procs={}).is_proxy())

    def test_same_rule_whichever_source_answers(self):
        # A pid named by netstat or by nettop passes or fails the same
        # check: root AND the daemon's own executable.
        paths = (_SYSEXT, "/usr/local/bin/tailscaled", _GUI, _TS_CLI,
                 "/sbin/launchd", "/tmp/tailscaled-x", "")
        for path in paths:
            for uid in (0, 501, None):
                with self.subTest(path=path, uid=uid):
                    procs = {1645: (path, uid, (1, 0))}
                    sighted = self.host(procs=procs, table=_NETSTAT).is_proxy()
                    blind = self.host(procs=procs, listings={1645: _listing(
                        pid=1645, rows=[(_SERVE, "Established")])}).is_proxy()
                    self.assertEqual(sighted, blind)
                    self.assertEqual(sighted, uid == 0 and path in paths[:2])


def _can_run_ps() -> bool:
    """Whether this process may exec /bin/ps. It is setuid root, and the
    Nix build on macOS refuses to exec setuid programs (EPERM)."""
    try:
        subprocess.run([peer._PS, "-o", "pid=", "-p", str(os.getpid())],
                       capture_output=True, stdin=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


class TestDarwinProcessLookups(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin", "libproc and kern.proc are macOS's")
    def test_this_process_is_listed_and_identified(self):
        if not _can_run_ps():
            self.skipTest("this build cannot exec the setuid /bin/ps (the Nix build on macOS)")
        self.assertIn(os.getpid(), peer._all_pids())
        ident = peer._identity(os.getpid())
        self.assertIsNotNone(ident)
        path, uid, (sec, usec) = ident
        self.assertEqual(path, peer._pid_path(os.getpid()))
        self.assertEqual(uid, os.getuid())
        self.assertTrue(0 <= usec < 10 ** 6)
        # The start time is this process's, not the clock's.
        self.assertEqual(peer._identity(os.getpid()), ident)
        self.assertIsNone(peer._pid_start(2 ** 22 + 7))
        self.assertIsNone(peer._identity(2 ** 22 + 7))

    def test_pid_listing_that_fails_is_empty(self):
        with patch("ctypes.CDLL", side_effect=OSError("no libproc")):
            self.assertEqual(peer._all_pids(), [])

    def test_netstat_sees(self):
        self.assertTrue(peer.netstat_sees(_NETSTAT))
        self.assertFalse(peer.netstat_sees(""))
        self.assertFalse(peer.netstat_sees("\n\n"))
        self.assertFalse(peer.netstat_sees(
            "Active Internet connections (including servers)\n"))


class TestElsewhere(unittest.TestCase):
    def test_other_platforms_never_trust(self):
        with patch.object(peer.sys, "platform", "win32"):
            self.assertFalse(peer.is_proxy(("127.0.0.1", 1), ("127.0.0.1", 2)))

    def test_errors_are_not_trust(self):
        with patch.object(peer.sys, "platform", "linux"), \
                patch.object(peer, "linux_uid", side_effect=PermissionError):
            self.assertFalse(peer.is_proxy(("127.0.0.1", 1), ("127.0.0.1", 2)))


if __name__ == "__main__":
    unittest.main()
