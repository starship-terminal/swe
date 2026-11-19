"""Web app page plugins: dispatch of /v1/app/<page> and the page bundles."""

import http.client
import json
import shutil
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from swe.webapp import pages

from tests.test_remote_hub import HubBase

from tests._tempdir import temp_dir


def _page(get=None, post=None):
    mod = types.SimpleNamespace()
    if get:
        mod.get = get
    if post:
        mod.post = post
    return mod


class TestLoad(unittest.TestCase):
    def test_only_plain_names_of_real_modules(self):
        for bad in ("", "_private", "Upper", "a-b", "../x", "nope", "__init__"):
            self.assertIsNone(pages.load(bad), bad)

    def test_home_path_falls_back_to_the_process_home(self):
        # One rule for every page: the context's home, else the real one
        # (no context at all, or one naming none).
        self.assertEqual(
            pages.home_path(pages.PageContext(home=Path("/h"), state=None)),
            Path("/h"))
        self.assertEqual(pages.home_path(types.SimpleNamespace(home="/s")),
                         Path("/s"))
        self.assertEqual(
            pages.home_path(pages.PageContext(home=None, state=None)),
            Path.home())
        self.assertEqual(pages.home_path(None), Path.home())

    def test_tilde_writes_the_home_as_a_tilde(self):
        self.assertEqual(pages.tilde("/h/u", "/h/u/"), "~")
        self.assertEqual(pages.tilde("/h/u/a/b", "/h/u"), "~/a/b")
        self.assertEqual(pages.tilde("/h/user2/a", "/h/u"), "/h/user2/a")
        self.assertEqual(pages.tilde("/elsewhere", "/h/u"), "/elsewhere")
        with patch.object(pages.os, "sep", "\\"):
            self.assertEqual(pages.tilde("C:\\Users\\a\\.swe", "C:\\Users\\a"),
                             "~\\.swe")
            self.assertEqual(pages.tilde("C:\\Users\\a", "C:\\Users\\a\\"), "~")

    def test_bundle_concatenates_in_name_order(self):
        folder = temp_dir(self)
        (folder / "b.js").write_text("B")
        (folder / "a.js").write_text("A")
        (folder / "a.css").write_text("CSS")
        with patch.object(pages, "STATIC", folder):
            body = pages.bundle(".js").decode()
            self.assertLess(body.index("A"), body.index("B"))
            self.assertNotIn("CSS", body)
            self.assertIn("/* a.js */", body)
            self.assertEqual(pages.bundle(".none"), b"")

    def test_bundled_rebuilds_only_when_a_file_changes(self):
        import os
        folder = temp_dir(self)
        page = folder / "a.js"
        page.write_text("A")
        real = pages.bundle
        with patch.object(pages, "STATIC", folder), \
                patch.dict(pages._BUNDLES, clear=True), \
                patch.object(pages, "bundle", side_effect=real) as build:
            body, etag = pages.bundled(".js")
            self.assertEqual(pages.bundled(".js"), (body, etag))
            self.assertEqual(build.call_count, 1)
            page.write_text("AB")
            os.utime(page, ns=(1, 1))  # a changed mtime and size
            body2, etag2 = pages.bundled(".js")
        self.assertEqual(build.call_count, 2)
        self.assertNotEqual(etag, etag2)
        self.assertIn(b"AB", body2)

    def test_etag_matches_by_weak_comparison(self):
        tag = '"abc"'
        for header in ('"abc"', 'W/"abc"', '"x", "abc"', "*", ' W/"abc" ,"y"'):
            self.assertTrue(pages.etag_matches(header, tag), header)
        for header in (None, "", '"abd"', 'W/"x"'):
            self.assertFalse(pages.etag_matches(header, tag), header)

    def test_concurrent_requests_build_once(self):
        import threading
        folder = temp_dir(self)
        (folder / "a.js").write_text("A")
        gate, built = threading.Event(), []
        real = pages.bundle

        def slow(suffix):
            built.append(suffix)
            gate.wait(5)
            return real(suffix)

        # The bundle lock, counting the requests that reach it: the build
        # is held until all three are at the lock, so none can come late.
        arrived = threading.Semaphore(0)

        class CountingLock:
            def __init__(self):
                self.lock = threading.Lock()

            def __enter__(self):
                arrived.release()
                self.lock.acquire()

            def __exit__(self, *exc):
                self.lock.release()

        with patch.object(pages, "STATIC", folder), \
                patch.dict(pages._BUNDLES, clear=True), \
                patch.object(pages, "_BUNDLES_LOCK", CountingLock()), \
                patch.object(pages, "bundle", side_effect=slow):
            threads = [threading.Thread(target=pages.bundled, args=(".js",))
                       for _ in range(3)]
            for t in threads:
                t.start()
            try:
                for _ in threads:
                    self.assertTrue(arrived.acquire(timeout=5), "a request never reached the lock")
            finally:
                # Even on a failure, no worker outlives the patches.
                gate.set()
                for t in threads:
                    t.join(5)
        self.assertEqual(built, [".js"])

    def test_a_broken_page_does_not_stop_the_others(self):
        """One page that won't parse and one that throws are logged and
        skipped; the pages around them still register."""
        import shutil
        import subprocess
        folder = temp_dir(self)
        (folder / "a.js").write_text('registerPage("a", () => "a");\n')
        (folder / "b.js").write_text('registerPage("b", () => {;\n')        # syntax error
        (folder / "c.js").write_text('throw new Error("boom");\n')         # throws on load
        (folder / "d.js").write_text('const label = "d";\nregisterPage("d", () => label);\n')
        with patch.object(pages, "STATIC", folder):
            body = pages.bundle(".js").decode()
        node = shutil.which("node")
        if node is None:
            self.skipTest("node not installed")
        script = ("const PAGES = {}; function registerPage(id, r) { PAGES[id] = r; }\n"
                  "const logged = []; console.error = (...a) => logged.push(String(a[0]));\n"
                  + body + "\nprocess.stdout.write(Object.keys(PAGES).join(',') + '|' + logged.length);")
        out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.stdout, "a,d|2", out.stderr)

    def test_js_string_round_trips(self):
        import shutil
        import subprocess
        node = shutil.which("node")
        if node is None:
            self.skipTest("node not installed")
        for text in ('say "hi"', "it's", "back\\slash", "two\nlines\r\n", "tab\t",
                     "sep\u2028x\u2029", "`${template}`", "</script>"):
            # Bytes, not text mode: text mode would fold the \r\n we test.
            out = subprocess.run([node, "-e", "process.stdout.write(" + pages._js_string(text) + ")"],
                                 capture_output=True, timeout=30)
            self.assertEqual(out.stdout.decode("utf-8"), text)
        self.assertIn('registerPage("x"', pages._js_string('registerPage("x", r)'))

    def test_each_script_gets_its_own_scope(self):
        """Two pages reusing private names both register their own render."""
        import shutil
        import subprocess
        folder = temp_dir(self)
        for page in ("one", "two"):
            (folder / f"{page}.js").write_text(
                '"use strict";\nconst label = "%s";\n'
                "function render() { return label; }\n"
                'registerPage("%s", render);\n' % (page, page))
        with patch.object(pages, "STATIC", folder):
            body = pages.bundle(".js").decode()
        node = shutil.which("node")
        if node is None:
            self.skipTest("node not installed")
        script = ("const PAGES = {}; function registerPage(id, r) { PAGES[id] = r; }\n" + body
                  + "\nprocess.stdout.write(PAGES.one() + ',' + PAGES.two());")
        out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.stdout, "one,two", out.stderr)


class TestShippedBundle(unittest.TestCase):
    """The real scripts, in index.html's order, then the served page
    bundle, each run as its own classic script under node with a
    permissive DOM: no script fails to parse or throws while loading,
    and every page that calls registerPage registers. One test for every
    page, instead of a substring check per page."""

    STATIC = Path(pages.__file__).resolve().parent.parent / "static"
    RUNNER = r"""
const vm = require("vm"), fs = require("fs");
const [order, bundlePath] = JSON.parse(process.argv[1]);
// Anything the scripts touch at load: every property and call answers
// with the same stub, so only a real parse error or throw can fail.
const any = new Proxy(function () {}, {
  get: (_t, k) => (k === Symbol.toPrimitive ? () => "" : k === Symbol.iterator ? function* () {} : any),
  apply: () => any, construct: () => any, set: () => true, deleteProperty: () => true,
});
globalThis.window = globalThis;
globalThis.addEventListener = () => {};
globalThis.document = any;
globalThis.navigator = { language: "en-US", userAgent: "node" };
globalThis.location = { hash: "" };
globalThis.history = any;
const store = new Map();
globalThis.localStorage = { getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)), removeItem: (k) => store.delete(k) };
globalThis.matchMedia = () => ({ matches: false, addEventListener() {} });
const errors = [];
console.error = (...a) => errors.push(a.map((x) => String(x && x.stack || x)).join(" "));
for (const file of order) vm.runInThisContext(fs.readFileSync(file, "utf8"), { filename: file });
vm.runInThisContext(fs.readFileSync(bundlePath, "utf8"), { filename: "pages.js" });
// Exit now: a page may arm a timer at load, and those never ran in this check.
process.stdout.write(JSON.stringify({ errors, pages: Object.keys(PAGES).sort() }), () => process.exit(0));
"""

    def test_every_page_loads_and_registers(self):
        import re
        node = shutil.which("node")
        if node is None:
            self.skipTest("node not installed")
        html = (self.STATIC / "index.html").read_text(encoding="utf-8")
        order = [str(self.STATIC / name) for name in re.findall(r'<script src="/([\w.-]+\.js)"', html)
                 if name != "pages.js"]
        with tempfile.NamedTemporaryFile("wb", suffix=".js", delete=False) as f:
            f.write(pages.bundle(".js"))
        self.addCleanup(Path(f.name).unlink)
        done = subprocess.run([node, "-e", self.RUNNER, json.dumps([order, f.name])],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        out = json.loads(done.stdout)
        self.assertEqual(out["errors"], [])
        # The pages the app ships, named here so a page that stops calling
        # registerPage (or renames itself) fails too, not only one that
        # calls it and breaks.
        shipped = {"agents", "calendar", "compute", "doctor", "hooks", "inbox", "mcp", "models", "plugins",
                   "pricing", "repositories", "settings", "setup", "skills", "status", "tasks"}
        self.assertLessEqual(shipped, set(out["pages"]))
        # And any page added since registers as well.
        declared = set()
        for path in (self.STATIC / "pages").glob("*.js"):
            declared |= set(re.findall(r'registerPage\("(\w+)"', path.read_text(encoding="utf-8")))
        self.assertLessEqual(declared, set(out["pages"]))


class TestBundleRoute(HubBase):
    def test_an_unchanged_bundle_answers_304(self):
        for name in ("pages.js", "pages.css"):
            conn = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=5)
            try:
                conn.request("GET", f"/{name}")
                resp = conn.getresponse()
                resp.read()
                etag = resp.getheader("ETag")
                self.assertEqual(resp.status, 200)
                self.assertTrue(etag)
                conn.request("GET", f"/{name}", headers={"If-None-Match": etag})
                again = conn.getresponse()
                self.assertEqual((again.status, again.read()), (304, b""))
                self.assertEqual(again.getheader("ETag"), etag)
            finally:
                conn.close()


class TestStylesheets(unittest.TestCase):
    """The web app's stylesheets against its own rules."""

    STATIC = Path(pages.__file__).resolve().parent.parent / "static"

    def sheets(self):
        return {p.name: p.read_text(encoding="utf-8")
                for p in [self.STATIC / "tokens.css", self.STATIC / "styles.css", *sorted((self.STATIC / "pages").glob("*.css"))]}

    def test_every_variable_used_is_defined(self):
        """A var() with no definition renders the property's initial
        value (chat's approval toggle had black text in dark mode)."""
        import re
        scripts = [p.read_text(encoding="utf-8") for p in [*self.STATIC.glob("*.js"), *self.STATIC.glob("pages/*.js")]]
        defined = set()
        for text in [*self.sheets().values(), *scripts]:  # scripts set some inline, as style="--c: …"
            defined |= set(re.findall(r"(--[a-z0-9-]+)\s*:", text))
        missing = sorted({f"{name}: {v}" for name, text in self.sheets().items()
                          for v in re.findall(r"var\((--[a-z0-9-]+)\)?", text) if v not in defined})
        self.assertEqual(missing, [])

    def test_no_all_caps_labels(self):
        """Sentence case only, as the Mac app."""
        self.assertEqual([n for n, t in self.sheets().items() if "uppercase" in t], [])


class TestLedgerAccess(HubBase):
    def test_pages_share_the_ledger(self):
        from swe.webapp.api import WebApp
        app = WebApp(self.srv.home, download_prices=False)
        self.addCleanup(app.close)
        with app.ledger() as (store, catalog, scan_ok):
            self.assertTrue(scan_ok)
            self.assertEqual(store.models(), [])
            self.assertIsNotNone(catalog)


class TestDispatch(HubBase):
    def test_get_passes_context_and_flat_args(self):
        seen = {}

        def get(ctx, args):
            seen["ctx"], seen["args"] = ctx, args
            return {"ok": True}
        with patch.object(pages, "load", return_value=_page(get=get)):
            self.assertEqual(self.srv.request("GET", "/v1/app/demo")[0], 401)
            status, data = self.srv.request("GET", "/v1/app/demo?x=1&x=2&y=", headers=self.auth())
        self.assertEqual((status, json.loads(data)), (200, {"ok": True}))
        self.assertEqual(seen["args"], {"x": "1"})
        self.assertIs(seen["ctx"].state, self.srv.state)
        self.assertEqual(seen["ctx"].machine, self.srv.state.machine)
        self.assertIsNotNone(seen["ctx"].device_id)

    def test_unknown_page_and_errors(self):
        status, _ = self.srv.request("GET", "/v1/app/nope", headers=self.auth())
        self.assertEqual(status, 404)

        def refuse(ctx, args):
            raise pages.PageError(409, "busy", "try later")

        def broken(ctx, args):
            raise OSError("disk gone")
        with patch.object(pages, "load", return_value=_page(get=refuse)):
            status, data = self.srv.request("GET", "/v1/app/demo", headers=self.auth())
        self.assertEqual(status, 409)
        self.assertIn("try later", data.decode())
        with patch.object(pages, "load", return_value=_page(get=broken)):
            status, _ = self.srv.request("GET", "/v1/app/demo", headers=self.auth())
        self.assertEqual(status, 503)

    def test_post_needs_a_device_an_action_and_a_good_origin(self):
        calls = []

        def post(ctx, action, body):
            calls.append((action, body))
            return {"done": action}
        hdr = {**self.auth(), "Content-Type": "application/json"}
        with patch.object(pages, "load", return_value=_page(post=post)):
            self.assertEqual(self.srv.request("POST", "/v1/app/demo/star", {"name": "x"})[0], 401)
            self.assertEqual(self.srv.request("POST", "/v1/app/demo", {}, headers=hdr)[0], 404)
            bad = {**hdr, "Origin": "https://evil.example"}
            self.assertEqual(self.srv.request("POST", "/v1/app/demo/star", {}, headers=bad)[0], 403)
            status, data = self.srv.request("POST", "/v1/app/demo/star", {"name": "x"}, headers=hdr)
        self.assertEqual((status, json.loads(data)), (200, {"done": "star"}))
        self.assertEqual(calls, [("star", {"name": "x"})])

    def test_page_without_post_is_404(self):
        with patch.object(pages, "load", return_value=_page(get=lambda c, a: {})):
            status, _ = self.srv.request("POST", "/v1/app/demo/x", {}, headers=self.auth())
        self.assertEqual(status, 404)

    def test_bundles_are_served(self):
        for path, ctype in (("/pages.js", "javascript"), ("/pages.css", "text/css")):
            conn = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=5)
            self.addCleanup(conn.close)
            conn.request("GET", path)
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 200, path)
            self.assertIn(ctype, resp.getheader("Content-Type", ""), path)
        status, body = self.srv.request("GET", "/")
        self.assertIn(b"/pages.js", body)


if __name__ == "__main__":
    unittest.main()
