#!/usr/bin/env python3
"""Check that a session keeper outlives a restart of the hub's service.

    python3 scripts/keeper_survival_check.py [--control] [--keep]

The real thing, against the real service manager: it installs a
throwaway service (a LaunchAgent on macOS, a systemd user unit on
Linux) built by the same code that builds the hub's, whose program is a
stand-in hub. The stand-in does what a hub does at start: reconnect to
every live keeper it can discover, else spawn one. The check then

1. waits for the stand-in to spawn a keeper (driving the fake ``claude``
   from tests/fakes),
2. restarts the service, and requires the new stand-in to find the same
   keeper alive and talk to it,
3. stops the service, and requires the keeper to be alive still,
4. stops the keeper and removes everything it made.

``--control`` runs the same steps with the protection taken away (no
``AbandonProcessGroup`` on macOS, no scope unit on Linux) and reports
what happens to the keeper then; it never fails the run. ``--keep``
leaves the work folder for a look.

Nothing here touches the real hub, its service or ``~/.swe``: the
service has its own label, and all state lives in a folder under /tmp
(a LaunchAgent cannot read a checkout under ~/Desktop, so the sources
are copied there first).
"""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SESSION = "claude:survival-check"
LABEL = "com.cwenlong.swe.keeper-survival-check"
UNIT = "swe-keeper-survival-check.service"
WAIT_S = 60.0


# ---------------------------------------------------------------------
# The stand-in hub (runs under the service manager)


def hub(work: Path, protect: bool) -> int:
    sys.path.insert(0, str(work / "src"))
    from swe.remote.keeper import discovery

    home, runtime = work / "home", work / "rt"
    alive = discovery.discover(home, alive_only=True)
    if alive:
        info, how = alive[0], "reconnected"
    else:
        scope = None if protect else (lambda slug: None)
        info = discovery.spawn(SESSION, home=home, runtime=runtime,
                               scope=scope)
        how = "spawned"
    client = discovery.connect(info)
    status = client.request("status")
    client.close()
    line = {"hub": os.getpid(), "keeper": info.pid, "how": how,
            "agent": status.get("agent"), "last_seq": status.get("last_seq")}
    with open(work / "hub.log", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")
    signal.pause()
    return 0


# ---------------------------------------------------------------------
# The check


def log(message: str) -> None:
    print(message, flush=True)


def run(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(list(argv), capture_output=True, text=True,
                          stdin=subprocess.DEVNULL)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def hub_lines(work: Path) -> list[dict]:
    try:
        text = (work / "hub.log").read_text(encoding="utf-8")
    except OSError:
        return []
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def wait_for(what: str, test, timeout: float = WAIT_S):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = test()
        if value:
            return value
        time.sleep(0.2)
    raise SystemExit(f"FAIL: timed out waiting for {what}")


class Mac:
    name = "launchd"

    def __init__(self, work: Path, program: list[str], protect: bool) -> None:
        sys.path.insert(0, str(REPO / "src"))
        from swe.remote import launchd

        self.domain = f"gui/{os.getuid()}"
        self.plist = work / f"{LABEL}.plist"
        plist = launchd.build_plist(program, work / "service.log",
                                    os.environ.get("PATH", ""), label=LABEL)
        if not protect:
            del plist["AbandonProcessGroup"]
        self.plist.write_bytes(plistlib.dumps(plist))
        self.protect = plist.get("AbandonProcessGroup") is True

    def describe(self) -> str:
        return f"AbandonProcessGroup={'true' if self.protect else 'absent'}"

    def start(self) -> None:
        run("launchctl", "bootout", f"{self.domain}/{LABEL}")
        res = run("launchctl", "bootstrap", self.domain, str(self.plist))
        if res.returncode != 0:
            raise SystemExit(f"FAIL: launchctl bootstrap: {res.stderr}")

    def restart(self) -> None:
        res = run("launchctl", "kickstart", "-k", f"{self.domain}/{LABEL}")
        if res.returncode != 0:
            raise SystemExit(f"FAIL: launchctl kickstart: {res.stderr}")

    def stop(self) -> None:
        run("launchctl", "bootout", f"{self.domain}/{LABEL}")

    def where(self, pid: int) -> str:
        res = run("ps", "-o", "pid=,ppid=,pgid=,sess=", "-p", str(pid))
        return "pid ppid pgid sess = " + " ".join(res.stdout.split())


class Linux:
    name = "systemd"

    def __init__(self, work: Path, program: list[str], protect: bool) -> None:
        sys.path.insert(0, str(REPO / "src"))
        from swe.remote import systemd

        if not systemd.available():
            raise SystemExit("SKIP: no systemd user manager on this login")
        self.systemd = systemd
        self.path = systemd.unit_path(unit=UNIT)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(systemd.build_unit(
            program, work / "service.log", os.environ.get("PATH", ""),
            "SWE keeper survival check"), encoding="utf-8")
        self.protect = protect

    def describe(self) -> str:
        return "keeper scope unit " + ("on" if self.protect else "off")

    def ctl(self, *args: str) -> subprocess.CompletedProcess:
        return run("systemctl", "--user", *args)

    def start(self) -> None:
        self.ctl("daemon-reload")
        res = self.ctl("restart", UNIT)
        if res.returncode != 0:
            raise SystemExit(f"FAIL: systemctl restart: {res.stderr}")

    def restart(self) -> None:
        res = self.ctl("restart", UNIT)
        if res.returncode != 0:
            raise SystemExit(f"FAIL: systemctl restart: {res.stderr}")

    def stop(self) -> None:
        self.ctl("stop", UNIT)
        self.path.unlink(missing_ok=True)
        self.ctl("daemon-reload")

    def where(self, pid: int) -> str:
        try:
            cgroup = Path(f"/proc/{pid}/cgroup").read_text().strip()
        except OSError:
            cgroup = "?"
        return "cgroup " + cgroup.splitlines()[-1]


def check(protect: bool, keep: bool) -> bool:
    """One pass. True when the keeper outlived the restart and the stop."""
    work = Path(tempfile.mkdtemp(prefix="swk", dir="/tmp"))
    home = work / "home"
    shutil.copytree(REPO / "src" / "swe", work / "src" / "swe",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(REPO / "tests" / "fakes" / "fake_claude.py", work)
    shutil.copy(Path(__file__).resolve(), work / "check.py")
    config = home / ".swe" / "config"
    config.mkdir(parents=True)
    (config / "harness.json").write_text(json.dumps({"claude": {
        "capabilities": {"session": {
            "adapter": "claude_stream",
            "argv": [sys.executable, str(work / "fake_claude.py")]}}}}))
    sys.path.insert(0, str(work / "src"))
    from swe.remote.keeper import discovery
    from swe.remote.protocol import key_slug

    discovery.write_spec(SESSION, harness="claude", cwd=str(work),
                         home=home)
    program = [sys.executable, str(work / "check.py"), "--hub", str(work)]
    if not protect:
        program.append("--no-protect")
    service = (Mac if sys.platform == "darwin" else Linux)(
        work, program, protect)
    log(f"== {service.name}, {service.describe()} ==")
    keeper = 0
    survived = False
    try:
        service.start()
        first = wait_for("the stand-in hub to spawn a keeper",
                         lambda: hub_lines(work))[0]
        keeper = first["keeper"]
        log(f"hub {first['hub']} {first['how']} keeper {keeper} "
            f"(agent {first['agent']}); {service.where(keeper)}")
        if sys.platform.startswith("linux") and protect:
            unit = f"swe-keeper-{key_slug(SESSION)}.scope"
            if unit not in service.where(keeper):
                raise SystemExit(f"FAIL: the keeper is not in {unit}")

        service.restart()
        try:
            second = wait_for("the restarted hub to report",
                              lambda: hub_lines(work)[1:], 45.0)[0]
        except SystemExit:
            if protect:
                raise
            second = {"hub": 0, "keeper": 0, "how": "nothing"}
        same = second["keeper"] == keeper and second["how"] == "reconnected"
        log(f"after a restart: hub {second['hub']} {second['how']} keeper "
            f"{second['keeper']}; first keeper alive: {alive(keeper)}")
        service.stop()
        time.sleep(2.0)
        still = alive(keeper)
        log(f"after the service stopped: first keeper alive: {still}")
        info = discovery.find(SESSION, home)
        answers = False
        if same and still and info is not None and info.alive:
            client = discovery.connect(info)
            answers = client.request("status").get("pid") == keeper
            client.request("stop", command_id="survival-check-stop")
            client.close()
        survived = same and still and answers
    finally:
        service.stop()
        for info in discovery.discover(home, alive_only=True):
            try:
                os.kill(info.pid, signal.SIGTERM)
            except OSError:
                pass
        if keep:
            log(f"kept {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)
    return survived


def main(argv: list[str]) -> int:
    if "--hub" in argv:
        work = Path(argv[argv.index("--hub") + 1])
        return hub(work, "--no-protect" not in argv)
    keep = "--keep" in argv
    if not check(True, keep):
        log("FAIL: the keeper did not outlive the hub's service")
        return 1
    log("PASS: the keeper outlived a restart and a stop of the hub's "
        "service, and still answered")
    if "--control" in argv:
        outlived = check(False, keep)
        log("control (protection off): the keeper "
            + ("survived anyway" if outlived else "did not survive"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
