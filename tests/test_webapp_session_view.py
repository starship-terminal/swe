"""The React session view (app/web-session) as the hub serves it: the
``/session/`` route, its cache and security headers, the name check that
keeps it inside the committed bundle, the fallback when the bundle is
missing, the bundle's freshness against its source, and the shared
tokens.css."""

import hashlib
import http.client
import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from swe.webapp import static

from tests._tempdir import temp_dir
from tests.test_remote_hub import HubBase

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "app" / "web-session"
BUNDLE = static.SESSION_DIR
INFO = json.loads((BUNDLE / "build-info.json").read_text(encoding="utf-8"))
CHUNKS = sorted(n for n in INFO["outputs"] if n.startswith("assets/"))
DEFAULT_CSP = "frame-ancestors 'self'; object-src 'none'; base-uri 'none'"


def source_files(root: Path) -> dict:
    """app/web-session/scripts/source-hash.mjs's rule, in Python: every
    file but dot names, node_modules, *.md and *.tsbuildinfo, by relative
    path, with the sha256 of its bytes."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "node_modules"
                       and not os.path.islink(os.path.join(dirpath, d))]
        for name in filenames:
            path = Path(dirpath) / name
            if name.startswith(".") or name.endswith((".md", ".tsbuildinfo")) or path.is_symlink():
                continue
            rel = path.relative_to(root).as_posix()
            out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return dict(sorted(out.items()))


def source_hash(files: dict) -> str:
    text = "".join(f"{rel}\0{files[rel]}\n" for rel in sorted(files))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class TestBundle(unittest.TestCase):
    """The committed build, as files."""

    @unittest.skipUnless(SOURCE.is_dir(), "app/web-session is not in this tree (sdist or Nix source)")
    def test_the_bundle_was_built_from_the_source_here(self):
        files = source_files(SOURCE)
        stale = sorted(set(files.items()) ^ set(INFO["sources"].items()))
        self.assertEqual(
            (source_hash(files), stale), (INFO["source_hash"], []),
            "the session view bundle is stale: run `pnpm build` in app/web-session "
            "(after a rebase too) and commit src/swe/webapp/static/session")

    def test_build_info_lists_exactly_the_files_built(self):
        on_disk = sorted(p.relative_to(BUNDLE).as_posix() for p in BUNDLE.rglob("*") if p.is_file())
        self.assertEqual(on_disk, INFO["outputs"])
        for name in ("index.html", "session-view.js", "session-view.css", "appearance.js", "build-info.json",
                     "third-party-licenses.md"):
            self.assertIn(name, INFO["outputs"])
        # React's MIT notice ships with it: the minifier drops the banner.
        notices = (BUNDLE / "third-party-licenses.md").read_text(encoding="utf-8")
        self.assertIn("## react - ", notices)
        self.assertIn("Copyright (c) Meta Platforms", notices)

    def test_chunks_carry_a_content_hash_and_the_rest_keep_stable_names(self):
        self.assertTrue(CHUNKS)
        for name in CHUNKS:
            self.assertRegex(name, r"^assets/[\w.-]+-[A-Za-z0-9_-]{8}\.(?:js|css)$")
        self.assertEqual([n for n in INFO["outputs"] if "/" in n and not n.startswith("assets/")], [])

    def test_the_page_runs_no_inline_code_and_loads_tokens_first(self):
        page = (BUNDLE / "index.html").read_text(encoding="utf-8")
        # script-src 'self': every script is a file.
        self.assertNotRegex(page, r"<script(?![^>]*\bsrc=)[^>]*>")
        self.assertNotIn("style=", page)
        refs = re.findall(r'(?:src|href)="([^"]+)"', page)
        for ref in refs:
            self.assertTrue(ref.startswith("/session/") or ref in ("/tokens.css", "/favicon.svg"), ref)
            if ref.startswith("/session/"):
                self.assertIn(ref[len("/session/"):], INFO["outputs"], ref)
        sheets = re.findall(r'<link rel="stylesheet"[^>]*href="([^"]+)"', page)
        self.assertEqual(sheets[0], "/tokens.css")
        self.assertIn('src="/session/appearance.js"', page)

    def test_base_styles_stay_inside_the_view_and_no_arbitrary_values_ship(self):
        css = (BUNDLE / "session-view.css").read_text(encoding="utf-8")
        start = css.index("@layer base{") + len("@layer base{")
        depth, selector, selectors = 0, "", []
        for ch in css[start:]:
            if ch == "{":
                if depth == 0:
                    selectors.append(selector.strip())
                depth, selector = depth + 1, ""
            elif ch == "}":
                if depth == 0:
                    break
                depth -= 1
            elif depth == 0:
                selector += ch
        self.assertGreater(len(selectors), 5)
        for group in selectors:
            # Split on the commas outside :where(...) and the like.
            for sel in re.split(r",(?![^()]*\))", group):
                self.assertTrue(sel.strip().startswith(".swe-sv"), sel)
        # An arbitrary value compiles to an escaped bracket in the selector.
        self.assertNotIn("\\[", css)
        self.assertNotIn("uppercase", css)


class TestNameCheck(unittest.TestCase):
    """session_file admits a name only if the build lists it and it is a
    plain file inside the bundle."""

    def setUp(self):
        self.base = temp_dir(self) / "session"
        (self.base / "assets").mkdir(parents=True)
        for rel in ("index.html", "session-view.js", "assets/a-AbCdEf12.js", "unlisted.js", ".hidden.js"):
            (self.base / rel).write_text("x")
        self.outside = temp_dir(self) / "secret.js"
        self.outside.write_text("secret")
        os.symlink(self.outside, self.base / "linked.js")
        os.symlink(self.base / "assets", self.base / "alias")
        outputs = ["index.html", "session-view.js", "assets/a-AbCdEf12.js", "linked.js",
                   "alias/a-AbCdEf12.js", ".hidden.js", "build-info.json"]
        (self.base / "build-info.json").write_text(json.dumps({"outputs": outputs}))

    def test_listed_files_are_served(self):
        for rel, want in (("", "index.html"), ("index.html", "index.html"),
                          ("session-view.js", "session-view.js"),
                          ("assets/a-AbCdEf12.js", "assets/a-AbCdEf12.js")):
            self.assertEqual(static.session_file(rel, self.base), self.base / want, rel)

    def test_everything_else_is_refused(self):
        for rel in ("..", "../app.js", "../../hub.py", "assets/../index.html", "assets/..",
                    "%2e%2e/app.js", "..%2Fapp.js", "assets%2Fa-AbCdEf12.js", "assets%2f..%2findex.html",
                    "assets\\a-AbCdEf12.js", "/etc/passwd", "//etc/passwd", "/index.html",
                    str(self.outside), ".hidden.js", "assets/.x.js", "unlisted.js", "nope.js",
                    "assets", "assets/", "x/y.js", "assets/x/y.js", "linked.js", "alias/a-AbCdEf12.js",
                    "index.html\0.js", "index.html?x", "index.html#x", " index.html"):
            self.assertIsNone(static.session_file(rel, self.base), rel)

    def test_a_missing_or_broken_build_info_admits_nothing(self):
        (self.base / "build-info.json").write_text("{not json")
        self.assertIsNone(static.session_file("index.html", self.base))
        self.assertFalse(static.session_built(self.base))
        (self.base / "build-info.json").unlink()
        self.assertIsNone(static.session_file("index.html", self.base))

    def test_the_real_bundle_is_built(self):
        self.assertTrue(static.session_built())


class TestTokens(unittest.TestCase):
    """The token blocks moved to tokens.css, which every page loads first."""

    STATIC = static.STATIC_DIR

    def test_index_links_tokens_before_styles(self):
        page = (self.STATIC / "index.html").read_text(encoding="utf-8")
        sheets = re.findall(r'<link rel="stylesheet" href="([^"]+)"', page)
        self.assertEqual(sheets[:2], ["/tokens.css", "/styles.css"])

    def test_tokens_hold_every_palette_and_styles_none(self):
        tokens = (self.STATIC / "tokens.css").read_text(encoding="utf-8")
        styles = (self.STATIC / "styles.css").read_text(encoding="utf-8")
        for block in (":root {", "@media (prefers-color-scheme: dark)", ':root:not([data-theme="light"])',
                      ':root[data-theme="dark"]'):
            self.assertIn(block, tokens)
            self.assertNotIn(block, styles)
        self.assertNotRegex(re.sub(r"/\*.*?\*/", "", tokens, flags=re.S), r"\}\s*[^:@\s}]")
        moved = set(re.findall(r"(--[\w-]+)\s*:", tokens))
        self.assertGreater(len(moved), 40)
        self.assertEqual(sorted(moved & set(re.findall(r"(--[\w-]+)\s*:", styles))), [])

    def test_the_session_view_maps_the_tokens_it_names(self):
        if not SOURCE.is_dir():
            self.skipTest("app/web-session is not in this tree")
        tokens = (self.STATIC / "tokens.css").read_text(encoding="utf-8")
        theme = (SOURCE / "src" / "index.css").read_text(encoding="utf-8")
        defined = set(re.findall(r"(--[\w-]+)\s*:", tokens))
        used = set(re.findall(r"var\((--[\w-]+)\)", theme))
        self.assertTrue(used)
        self.assertEqual(sorted(used - defined), [])
        self.assertIn("--color-*: initial;", theme)


class TestPackaging(unittest.TestCase):
    def test_nothing_ignores_the_bundle(self):
        if not (REPO / ".git").exists() or not shutil.which("git"):
            self.skipTest("not a git checkout")
        paths = [str(BUNDLE / n) for n in INFO["outputs"]]
        done = subprocess.run(["git", "-C", str(REPO), "check-ignore", *paths],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(done.stdout, "")

    def test_the_wheel_ships_the_python_package_whole(self):
        pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
        self.assertRegex(pyproject, r'packages = \["src/swe"')


class TestRoutes(HubBase):
    def get(self, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=5)
        try:
            conn.request("GET", path, headers=headers or {})
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()

    def test_the_page_is_public_like_the_root(self):
        status, headers, body = self.get("/session/")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertIn(b'<div id="root"></div>', body)
        self.assertEqual(body, (BUNDLE / "index.html").read_bytes())
        self.assertEqual(self.get("/session/?key=claude:s1")[2], body)
        self.assertEqual(self.get("/session/index.html")[0], 200)

    def test_bare_session_redirects_with_its_query(self):
        for path, where in (("/session", "/session/"), ("/session?key=claude:s1", "/session/?key=claude:s1")):
            status, headers, _ = self.get(path)
            self.assertEqual((status, headers.get("Location")), (301, where), path)

    def test_the_page_revalidates_and_chunks_are_immutable(self):
        status, headers, _ = self.get("/session/")
        self.assertEqual(headers["Cache-Control"], "no-cache")
        etag = headers["ETag"]
        self.assertRegex(etag, r'^"[0-9a-f]{32}"$')
        status, again, body = self.get("/session/", {"If-None-Match": etag})
        self.assertEqual((status, body, again["ETag"]), (304, b"", etag))
        for name in ("session-view.js", "session-view.css", "appearance.js"):
            status, headers, body = self.get("/session/" + name)
            self.assertEqual((status, headers["Cache-Control"]), (200, "no-cache"), name)
            self.assertIn("ETag", headers)
            self.assertEqual(body, (BUNDLE / name).read_bytes())
        for name in CHUNKS:
            status, headers, body = self.get("/session/" + name)
            self.assertEqual(status, 200, name)
            self.assertEqual(headers["Cache-Control"], "public, max-age=31536000, immutable")
            self.assertNotIn("ETag", headers)
            self.assertTrue(headers["Content-Type"].startswith(("text/javascript", "text/css")))

    def test_every_response_carries_the_session_policy(self):
        for path in ("/session/", "/session/session-view.js", "/session/" + CHUNKS[0]):
            headers = self.get(path)[1]
            self.assertEqual(headers["Content-Security-Policy"], static.SESSION_CSP, path)
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(headers["X-Frame-Options"], "SAMEORIGIN")
        self.assertTrue(static.SESSION_CSP.startswith(
            "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "font-src 'self'; connect-src 'self'; frame-src 'self'; base-uri 'none'; form-action 'none'"))
        # The rest of the hub keeps its own default policy.
        self.assertEqual(self.get("/")[1]["Content-Security-Policy"], DEFAULT_CSP)

    def test_nothing_outside_the_bundle_is_reachable(self):
        for path in ("/session/../app.js", "/session/%2e%2e/app.js", "/session/..%2Fapp.js",
                     "/session/%2E%2E%2Fapp.js", "/session/assets/../index.html",
                     "/session/assets/..%2F..%2Fapp.js", "/session//etc/passwd",
                     "/session/%2Fetc%2Fpasswd", "/session/.hidden", "/session/nope.js",
                     "/session/assets/", "/session/assets", "/session/static.py",
                     "/session/../../static.py", "/session/build-info.json/x"):
            status, headers, _ = self.get(path)
            self.assertEqual(status, 404, path)
            self.assertNotIn("Location", headers, path)

    def test_the_root_still_serves_the_old_app(self):
        status, _, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertEqual(body, (static.STATIC_DIR / "index.html").read_bytes())
        self.assertIn(b'src="/app.js"', body)
        for path in ("/tokens.css", "/styles.css"):
            self.assertEqual(self.get(path)[0], 200, path)

    def test_a_missing_bundle_answers_a_plain_page(self):
        empty = temp_dir(self) / "session"
        empty.mkdir()
        with patch.object(static, "SESSION_DIR", empty):
            status, headers, body = self.get("/session/")
            self.assertEqual(status, 503)
            self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertIn(b"session view is not built", body)
            self.assertNotIn(b"Traceback", body)
            self.assertEqual(self.get("/session/?key=claude:s1")[0], 503)
            self.assertEqual(self.get("/session/session-view.js")[0], 404)
        # A half-written bundle (page listed but gone) answers the same.
        partial = temp_dir(self) / "session"
        partial.mkdir()
        (partial / "build-info.json").write_text(json.dumps({"outputs": ["index.html"]}))
        with patch.object(static, "SESSION_DIR", partial):
            self.assertEqual(self.get("/session/")[0], 503)


if __name__ == "__main__":
    unittest.main()
