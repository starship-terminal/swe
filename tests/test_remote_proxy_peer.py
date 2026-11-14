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
        "this process cannot see socket owners on this host (macOS hides "
        "the TCP table from ad-hoc-signed programs and their children, "
        "#1944), so the real check can't be shown to name this process; "
        "it still fails closed")
    def test_local_process_cannot_claim_the_phone(self):
        # No stub: the real OS check sees this test process, not
        # tailscaled, on the far end of the loopback connection. The
        # socket-owner lookup is wrapped only to record its answer: a
        # lookup that fails also reads as "not the proxy" (fail closed),
        # so a 401 alone would not show the check works on this host.
        if sys.platform == "darwin":
            lookup, expected = "netstat_pid", os.getpid()
        else:
            lookup, expected = "linux_uid", os.getuid()
        real = getattr(peer, lookup)
        answers = []

        def recorded(*args, **kwargs):
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


def _proc_line(local, remote, uid, v6=False):
    enc = _hex6 if v6 else _hex4
    return (f"   0: {enc(local[0])}:{local[1]:04X} {enc(remote[0])}:{remote[1]:04X} "
            f"01 00000000:00000000 00:00000000 00000000 {uid:5d}        0 4242 1\n")


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
        ok, get_uid = self._check(1645, 0)
        self.assertTrue(ok)
        get_uid.assert_called_once_with(1645)

    def test_user_process_is_not(self):
        self.assertFalse(self._check(812, 501, "/Applications/Google Chrome.app/x")[0])

    def test_app_store_extension_counts_when_root_owned(self):
        path = "/Applications/Tailscale.app/Contents/PlugIns/IPNExtension.appex/x"
        with patch.object(peer, "app_store_binary", return_value=True) as ok_path:
            self.assertTrue(self._check(1645, 501, path)[0])
        ok_path.assert_called_once_with(path)

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
            _PROC_HEAD + "   0: short line\n" + short
            + good.replace(_hex4("127.0.0.1"), "ZZZZZZZZ", 1) + good)
        self.assertEqual(peer.linux_uid(self.hub, ("127.0.0.1", 50001), root), 0)

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
        # ``netstat -anv -p tcp``, compared with this process's pid.
        with patch.object(peer.sys, "platform", "darwin"), \
                patch.object(peer, "_run", return_value="") as run, \
                patch.object(peer, "netstat_pid",
                             side_effect=[os.getpid(), None]):
            self.assertTrue(peer.owner_lookup_works())
            self.assertFalse(peer.owner_lookup_works())
        run.assert_called_with([peer._NETSTAT, "-anv", "-p", "tcp"])

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
