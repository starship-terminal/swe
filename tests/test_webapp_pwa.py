"""What makes the web app installable and edge to edge: the manifest, the
theme-color metas and the safe-area padding."""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from tests.test_webapp import app_js_function

STATIC = Path(__file__).resolve().parents[1] / "src/swe/webapp/static"
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")
STYLES = (STATIC / "styles.css").read_text(encoding="utf-8")
TOKENS = (STATIC / "tokens.css").read_text(encoding="utf-8")
MANIFEST = json.loads((STATIC / "manifest.webmanifest").read_text(encoding="utf-8"))


def wash(scheme: str) -> str:
    """The page background tokens.css gives a scheme: the light default's
    --wash in the first rule, the dark one in the dark media query."""
    if scheme == "light":
        block = TOKENS.split("@media (prefers-color-scheme: dark)")[0]
    else:
        block = TOKENS.split("@media (prefers-color-scheme: dark)")[1]
    return re.search(r"--wash:\s*(#[0-9a-fA-F]{6})", block).group(1).lower()


class TestThemeColor(unittest.TestCase):
    def metas(self):
        return re.findall(r'<meta name="theme-color" content="([^"]+)" media="([^"]+)">', INDEX)

    def test_one_meta_per_scheme_matching_the_palette(self):
        self.assertEqual(
            sorted((content.lower(), media) for content, media in self.metas()),
            sorted([(wash("light"), "(prefers-color-scheme: light)"),
                    (wash("dark"), "(prefers-color-scheme: dark)")]))

    def test_no_bare_theme_color_is_left(self):
        self.assertEqual(len(re.findall(r'name="theme-color"', INDEX)), 2)

    def test_the_manifest_agrees_with_the_palette(self):
        self.assertEqual(MANIFEST["background_color"].lower(), wash("light"))
        self.assertEqual(MANIFEST["theme_color"].lower(), wash("light"))
        dark = MANIFEST["color_scheme_dark"]
        self.assertEqual(dark["background_color"].lower(), wash("dark"))
        self.assertEqual(dark["theme_color"].lower(), wash("dark"))


class TestManifest(unittest.TestCase):
    def test_it_installs_as_a_standalone_app(self):
        self.assertEqual(MANIFEST["display"], "standalone")
        self.assertEqual(MANIFEST["id"], "/")
        self.assertEqual(MANIFEST["start_url"], "/")
        self.assertTrue(MANIFEST["start_url"].startswith(MANIFEST["scope"]))

    def test_every_icon_exists(self):
        for icon in MANIFEST["icons"]:
            self.assertTrue((STATIC / icon["src"].lstrip("/")).is_file(), icon["src"])


class TestSafeArea(unittest.TestCase):
    def test_the_viewport_covers_the_screen_and_the_layout_pads_for_it(self):
        self.assertIn("viewport-fit=cover", INDEX)
        for side in ("top", "right", "bottom", "left"):
            self.assertIn("env(safe-area-inset-%s, 0px)" % side, TOKENS)
            self.assertIn("var(--safe-%s)" % side, STYLES)

    def test_the_edges_a_page_touches_are_padded(self):
        self.assertRegex(STYLES, r"\.sidebar \{[^}]*var\(--safe-top\)[^}]*var\(--safe-left\)")
        self.assertRegex(STYLES, r"\.page \{[^}]*var\(--safe-right\)[^}]*var\(--safe-bottom\)")
        chat = (STATIC / "pages/chat.css").read_text(encoding="utf-8")
        self.assertRegex(chat, r"\.chat-page \{[^}]*padding-top: var\(--safe-top\)[^}]*var\(--safe-bottom\)")
        # Narrow, the sidebar strip supplies the top inset, so the chat adds none.
        narrow = chat.split("@media (max-width: 720px) {", 1)[1]
        self.assertRegex(narrow, r"\.chat-page \{ padding-top: 0; \}")


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TestSyncThemeColor(unittest.TestCase):
    """A Light or Dark picked in Settings beats the system scheme, so the
    metas follow data-theme; System restores what the HTML said."""

    def run_steps(self, steps):
        prelude = (
            "const metas = [{ content: '#fcfcfc', dataset: {} }, { content: '#141414', dataset: {} }];\n"
            "const root = { dataset: {} };\n"
            "globalThis.document = { documentElement: root, querySelectorAll: () => metas };\n"
            "globalThis.getComputedStyle = () => ({ getPropertyValue: () => (root.dataset.theme === 'dark' ? ' #141414' : ' #fcfcfc') });\n")
        body = "\nconst out = [];\n" + steps + "\nprocess.stdout.write(JSON.stringify(out));\n"
        done = subprocess.run(["node", "-e", prelude + app_js_function("syncThemeColor") + body],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_forced_modes_override_both_metas_and_system_restores_them(self):
        got = self.run_steps(
            "const seen = () => out.push(metas.map((m) => m.content));\n"
            "syncThemeColor(); seen();\n"
            "root.dataset.theme = 'dark'; syncThemeColor(); seen();\n"
            "root.dataset.theme = 'light'; syncThemeColor(); seen();\n"
            "delete root.dataset.theme; syncThemeColor(); seen();\n")
        self.assertEqual(got, [["#fcfcfc", "#141414"], ["#141414", "#141414"],
                               ["#fcfcfc", "#fcfcfc"], ["#fcfcfc", "#141414"]])


if __name__ == "__main__":
    unittest.main()
