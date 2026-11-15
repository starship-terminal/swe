"""app/mac/script/bundle-licenses.sh: the licence texts SWE.app must carry."""

import os
import shutil
import subprocess
import unittest
from pathlib import Path

from tests._tempdir import temp_dir

MAC = Path(__file__).resolve().parents[1] / "app" / "mac"
SCRIPT = MAC / "script" / "bundle-licenses.sh"
# Resolved once against the test's own PATH: the Nix sandbox has no
# /bin/bash or /usr/bin, so neither a hardcoded path nor a lookup on a
# trimmed PATH finds it there.
BASH = shutil.which("bash")


@unittest.skipUnless(BASH, "bash is not installed")
class TestBundleLicenses(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir(self)
        self.src = self.tmp / "Licenses"
        shutil.copytree(MAC / "Licenses", self.src)
        self.sparkle = self.tmp / "SparkleLICENSE"
        self.sparkle.write_text("Sparkle licence\n")
        self.bundle = self.tmp / "SWE.app"

    def run_script(self, *args):
        # The script needs mkdir, cp and dirname, which live wherever the
        # test's PATH says (coreutils in the Nix store, /bin on a Mac), so
        # the PATH is passed through; the rest of the environment is not.
        env = {"PATH": os.environ.get("PATH", os.defpath),
               "SWE_LICENSES_SRC": str(self.src),
               "SWE_SPARKLE_LICENSE": str(self.sparkle)}
        return subprocess.run([BASH, str(SCRIPT), *args], capture_output=True,
                              text=True, env=env, timeout=60)

    def names(self):
        return self.run_script("--list").stdout.split()

    def test_every_listed_text_lands_in_the_bundle(self):
        done = self.run_script(str(self.bundle))
        self.assertEqual(done.returncode, 0, done.stderr)
        landed = sorted(p.name for p in (self.bundle / "Contents/Resources/Licenses").iterdir())
        self.assertEqual(landed, sorted(self.names()))
        self.assertEqual((self.bundle / "Contents/Resources/Licenses/sparkle.txt").read_text(),
                         "Sparkle licence\n")

    def test_a_missing_python_text_fails_even_when_others_remain(self):
        for name in self.names():
            if name == "sparkle.txt":
                continue
            with self.subTest(name):
                shutil.rmtree(self.bundle, ignore_errors=True)
                shutil.copy(MAC / "Licenses" / name, self.src / name)
                (self.src / name).unlink()
                done = self.run_script(str(self.bundle))
                self.assertNotEqual(done.returncode, 0)
                self.assertIn(name, done.stderr)
                shutil.copy(MAC / "Licenses" / name, self.src / name)

    def test_an_empty_text_fails(self):
        (self.src / "python-libffi.txt").write_text("")
        done = self.run_script(str(self.bundle))
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("python-libffi.txt", done.stderr)

    def test_a_missing_sparkle_licence_fails(self):
        self.sparkle.unlink()
        done = self.run_script(str(self.bundle))
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("swift build", done.stderr)

    def test_the_list_matches_the_folder_and_the_notices(self):
        listed = set(self.names())
        in_folder = {p.name for p in (MAC / "Licenses").glob("*.txt")}
        self.assertEqual(listed - {"sparkle.txt"}, in_folder)
        notices = (MAC / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        for name in listed:
            self.assertIn("`%s`" % name if name != "sparkle.txt" else "Licenses/sparkle.txt", notices, name)


if __name__ == "__main__":
    unittest.main()
