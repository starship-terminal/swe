"""scripts/demo_vm.sh: the UTM virtual machine for recording the primitives demo (#524).

A real run needs UTM and a macOS 27 Mac, so these tests stop short of the
VM: argument handling, the refusal on a machine that cannot run it, which
must leave nothing behind, and `create`'s first run up to the restore
image, with a stand-in Mac.
"""

import os
import shutil
import subprocess
import unittest
from pathlib import Path

from tests._tempdir import temp_dir

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "demo_vm.sh"
# Prefer macOS's bash 3.2, the oldest one the script promises to run on.
BASH = "/bin/bash" if os.path.exists("/bin/bash") else shutil.which("bash")


@unittest.skipUnless(SCRIPT.exists(), "scripts/ is not part of this source tree")
@unittest.skipUnless(BASH, "bash not installed")
class DemoVmTest(unittest.TestCase):
    def setUp(self):
        self.scratch = temp_dir(self).resolve()
        # No utmctl on this PATH, so every step refuses before touching a VM.
        # It does carry everything need_host runs before that check: in the
        # Nix build on a Mac sed and friends come from the store, and
        # sw_vers only from /usr/bin, so each tool's own folder is listed.
        self.bin = self.scratch / "bin"
        self.bin.mkdir()
        tool_dirs = {os.path.dirname(shutil.which(tool) or f"/usr/bin/{tool}")
                     for tool in ("sed", "uname", "dirname", "cut", "sw_vers")}
        self.path = ":".join([str(self.bin), *sorted(tool_dirs)])
        self.state = self.scratch / "state"
        self.cache = self.scratch / "cache"

    def _script(self, *args):
        env = {"PATH": self.path, "HOME": str(self.scratch / "home"),
               "SWE_DEMO_VM_STATE": str(self.state), "SWE_DEMO_VM_CACHE": str(self.cache)}
        return subprocess.run([BASH, str(SCRIPT), *args], capture_output=True, text=True,
                              env=env, timeout=60)

    def test_parses_under_the_oldest_bash(self):
        run = subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_help_lists_every_step(self):
        run = self._script("--help")
        self.assertEqual(run.returncode, 0, run.stderr)
        for step in ("create", "provision", "save", "take", "reset", "ssh", "shot", "record", "all"):
            self.assertIn(f"scripts/demo_vm.sh {step}", run.stderr)
        self.assertEqual(run.stdout, "")

    def test_no_step_prints_the_usage_and_fails(self):
        run = self._script()
        self.assertEqual(run.returncode, 1)
        self.assertIn("scripts/demo_vm.sh create", run.stderr)

    def test_unknown_step_is_refused(self):
        run = self._script("film")
        self.assertEqual(run.returncode, 1)
        self.assertIn("demo_vm: unknown command: film", run.stderr)

    def test_every_step_refuses_without_utm_and_writes_nothing(self):
        for step in ("create", "provision", "save", "take", "reset", "ssh", "shot", "record", "all"):
            with self.subTest(step=step):
                run = self._script(step)
                self.assertEqual(run.returncode, 1, run.stdout)
                # Linux CI stops at the platform check, a Mac at the missing utmctl.
                self.assertRegex(run.stderr, r"^demo_vm: (needs a Mac|needs macOS 27|utmctl is not on PATH)")
                self.assertFalse(self.state.exists())
                self.assertFalse(self.cache.exists())

    @unittest.skipUnless(shutil.which("ssh-keygen"), "ssh-keygen not installed")
    def test_first_create_makes_the_guest_key_and_password(self):
        # A stand-in macOS 27 Mac on Apple silicon whose UTM has no VMs, so
        # create gets past the host checks, makes the guest's key pair and
        # password, then stops at the restore image it was pointed at.
        shims = {"uname": 'case "$1" in -s) echo Darwin ;; -m) echo arm64 ;; esac',
                 "sw_vers": "echo 27.2",
                 "utmctl": "exit 1"}
        for name, body in shims.items():
            (self.bin / name).write_text(f"#!/bin/sh\n{body}\n")
            (self.bin / name).chmod(0o755)
        tool_dirs = {os.path.dirname(shutil.which(tool))
                     for tool in ("ssh-keygen", "head", "tr", "mkdir", "chmod", "cut", "basename")
                     if shutil.which(tool)}
        self.path = ":".join([str(self.bin), *sorted(tool_dirs), self.path])
        missing = self.scratch / "no-such.ipsw"
        run = subprocess.run([BASH, str(SCRIPT), "create"], capture_output=True, text=True,
                             env={"PATH": self.path, "HOME": str(self.scratch / "home"),
                                  "SWE_DEMO_VM_STATE": str(self.state),
                                  "SWE_DEMO_VM_CACHE": str(self.cache),
                                  "SWE_DEMO_VM_IPSW": str(missing)}, timeout=60)
        self.assertEqual(run.returncode, 1, run.stderr)
        self.assertIn(f"SWE_DEMO_VM_IPSW={missing} is not a file", run.stderr)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        password = (self.state / "demo-password").read_text()
        self.assertRegex(password, r"^demo-[a-z0-9]{8}$")
        self.assertEqual((self.state / "demo-password").stat().st_mode & 0o777, 0o600)
        self.assertTrue((self.state / "id_ed25519").is_file())
        self.assertTrue((self.state / "id_ed25519.pub").is_file())
        # No account yet: the first boot has not answered.
        self.assertFalse((self.state / "swe-demo-base.account").exists())


if __name__ == "__main__":
    unittest.main()
