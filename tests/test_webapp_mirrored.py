"""Mirrored sessions in the web app (#1918): a session whose harness
deleted its transcript and whose read-only copy swe kept.

``/v1/app/sessions`` rows carry ``mirrored``; the Sessions list tags such
a row and offers no Attach, Detach, Resume or Fork on it; its chat opens
with a one-line notice and falls back to a calm empty state when the hub
cannot read the copy yet. Live rows are left exactly as they were."""

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from swe.webapp import sessions

from tests.test_webapp_chat_queue import CHAT_JS, PRELUDE

STATIC = Path(__file__).resolve().parent.parent / "src" / "swe" / "webapp" / "static"
SESSIONS_JS = STATIC / "sessions.js"
HINT = "Its transcript was deleted by the harness. swe kept a read-only copy."


def _run_node(test, script):
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(script)
    test.addCleanup(Path(f.name).unlink)
    done = subprocess.run(["node", f.name], capture_output=True, text=True, timeout=30)
    test.assertEqual(done.returncode, 0, done.stderr)
    return json.loads(done.stdout)


class TestMirroredRows(unittest.TestCase):
    """The page data: what the hub's catalog says reaches the row."""

    def build(self, **extra):
        row = {"key": "claude:a", "id": "a", "harness": "claude", "machine_id": "hub",
               "last_active_at": 5, "status": None, "resume": None, **extra}
        return sessions.build([row], {}, "hub", {}, scanned=True)[0]

    def test_the_row_carries_mirrored(self):
        self.assertTrue(self.build(mirrored=True)["mirrored"])
        # An older hub or satellite row has no field: a live session.
        self.assertFalse(self.build()["mirrored"])

    def test_a_mirrored_row_offers_nothing_to_resume_attach_or_fork(self):
        row = self.build(mirrored=True, attachable=True, can_fork=True,
                         resume={"argv": ["claude", "--resume", "a"], "cwd": "/w"})
        self.assertEqual((row["resume"], row["attachable"], row["can_fork"]), (None, False, False))

    def test_a_live_row_is_unchanged(self):
        row = self.build(attachable=True, can_fork=True, status="done",
                         resume={"argv": ["claude", "--resume", "a"], "cwd": "/w"})
        self.assertEqual((row["attachable"], row["can_fork"], row["status"]), (True, True, "done"))
        self.assertEqual(row["resume"]["argv"], ["claude", "--resume", "a"])

    def test_no_usage_does_not_hide_a_mirrored_session_as_accidental(self):
        # Its source is gone, so the ledger has no usage for it; after a
        # clean scan a live claude row like that is judged empty.
        live = self.build()
        self.assertTrue(live["trivial"])
        self.assertFalse(self.build(mirrored=True)["trivial"])


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TestMirroredList(unittest.TestCase):
    """sessions.js under node with app.js's globals stubbed."""

    PRELUDE = (
        "const esc = (s) => String(s == null ? '' : s).replace(/&/g, '&amp;')"
        ".replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/\"/g, '&quot;');\n"
        "const created = [];\n"
        "const sheet = { innerHTML: '', querySelector: () => null, showModal() {} };\n"
        "globalThis.document = { getElementById: (id) => id === 'session-sheet' ? sheet : null,\n"
        "  createElement: () => { const m = { style: {}, offsetWidth: 120, offsetHeight: 80, innerHTML: '',\n"
        "    setAttribute() {}, querySelectorAll: () => [], contains: () => false, remove() {} }; created.push(m); return m; },\n"
        "  body: { appendChild() {} }, addEventListener() {}, removeEventListener() {} };\n"
        "globalThis.window = { innerWidth: 1000, innerHeight: 800, addEventListener() {}, removeEventListener() {} };\n"
        "globalThis.location = { hash: '' };\n"
        "globalThis.Fmt = { logo: () => '', harnessName: (h) => h, modelName: (m) => m || '',"
        " tokens: String, dollars: String, copyIcon: () => '', copyTick() {}, shellWord: String };\n"
        "globalThis.Sk = new Proxy({}, { get: () => () => '' });\n")

    def run_js(self, body):
        script = (self.PRELUDE + SESSIONS_JS.read_text(encoding="utf-8")
                  + "\n(async () => { const out = await (async () => {" + body + "})();"
                  + " process.stdout.write(JSON.stringify(out)); })()"
                  + ".catch((e) => { console.error(e); process.exit(1); });\n")
        return _run_node(self, script)

    ROWS = """
      const live = { key: "claude:live", id: "live", harness: "claude", title: "Live one", status: "active",
        attachable: true, can_fork: true, ownership: "observed", last_active_at: 5,
        resume: { argv: ["claude", "--resume", "live"] } };
      const gone = { key: "claude:gone", id: "gone", harness: "claude", title: "Pruned one", status: null,
        mirrored: true, attachable: true, can_fork: true, ownership: "attached", kept: true,
        last_active_at: 4, resume: null };
    """

    def test_a_mirrored_row_has_the_tag_and_no_live_marks_or_actions(self):
        out = self.run_js(self.ROWS + """
            return { gone: sessionRow(gone), live: sessionRow(live) };""")
        gone, live = out["gone"], out["live"]
        self.assertIn('class="badge mirrored"', gone)
        self.assertIn(f'title="{HINT}"', gone)
        self.assertIn(">Mirrored<", gone)
        for absent in ("row-act", "status-", "Attached", "Kept", "state-"):
            self.assertNotIn(absent, gone)
        # The other row actions stay: pin and rename still work on a copy.
        self.assertIn('class="row-more"', gone)
        # The tag sits in the badges' slot, after the title and inside it.
        self.assertRegex(gone, r'class="session-title">.*Pruned one</span><span class="badge mirrored"')

    def test_a_live_row_is_unchanged(self):
        out = self.run_js(self.ROWS + """
            return sessionRow(live);""")
        self.assertIn('class="badge status-active"', out)
        # Mid-turn, nothing to attach: the phone's rule is untouched.
        self.assertNotIn("row-act", out)
        self.assertNotIn("mirrored", out.lower())
        out = self.run_js(self.ROWS + """
            return sessionRow(Object.assign({}, live, { status: "done" }));""")
        self.assertIn('data-act="attach"', out)

    def test_no_attach_or_detach_for_a_mirrored_row_even_if_the_hub_says_so(self):
        out = self.run_js(self.ROWS + """
            return [rowAction(gone), rowAction(Object.assign({}, gone, { ownership: "attached" })),
                    rowAction(Object.assign({}, live, { status: "done" })), rowAction(live)];""")
        self.assertEqual(out, ["", "", "attach", ""])

    def test_the_menu_keeps_pin_and_rename_and_turns_fork_off_with_the_reason(self):
        out = self.run_js(self.ROWS + """
            const open = (r) => { created.length = 0; openSessionMenu(r, { getBoundingClientRect: () => ({ left: 0, right: 1, top: 0, bottom: 1 }),
              setAttribute() {}, contains: () => false }); return created[0].innerHTML; };
            return { actions: rowMenuActions(gone), goneMenu: open(gone), liveMenu: open(Object.assign({}, live, { key: "other" })) };""")
        self.assertEqual(out["actions"], ["pin", "rename"])
        self.assertIn('data-act="pin"', out["goneMenu"])
        self.assertIn('data-act="rename"', out["goneMenu"])
        self.assertRegex(out["goneMenu"], r'data-act="fork" aria-disabled="true"\s+title="' + re.escape(HINT))
        self.assertIn('data-act="fork">Fork', out["liveMenu"])
        self.assertNotIn("aria-disabled", out["liveMenu"])

    def test_fork_never_opens_for_a_mirrored_session(self):
        out = self.run_js(self.ROWS + """
            let opened = 0;
            openForkSheet = () => { opened++; };
            runSessionAction("fork", gone);
            const afterMirrored = opened;
            runSessionAction("fork", live);
            return [afterMirrored, opened];""")
        self.assertEqual(out, [0, 1])

    def test_the_details_sheet_says_why_and_offers_no_attach(self):
        out = self.run_js(self.ROWS + """
            openSheet(gone);
            const mirrored = sheet.innerHTML;
            openSheet(Object.assign({}, live, { status: "done" }));
            return { mirrored, live: sheet.innerHTML };""")
        self.assertIn(f'<p class="caption mirrored-note">{HINT}</p>', out["mirrored"])
        self.assertIn('class="badge mirrored"', out["mirrored"])
        self.assertNotIn('id="sheet-act"', out["mirrored"])
        self.assertNotIn("Resume", out["mirrored"])
        self.assertIn('id="sheet-act"', out["live"])
        self.assertNotIn("mirrored", out["live"])

    def test_a_mirrored_row_stays_in_the_list_and_its_counts(self):
        # There is no status filter on the page: tool, time, project and
        # model only, so the copy is kept by every filter a live row passes.
        out = self.run_js(self.ROWS + """
            sessionsPage.window = "all";
            sessionsPage.data = { sessions: [live, gone] };
            const { kept, trivial } = filteredSessions();
            return { kept: kept.map((r) => r.key), trivial, html: sessionsTable(kept) };""")
        self.assertEqual(out["kept"], ["claude:live", "claude:gone"])
        self.assertEqual(out["trivial"], 0)
        self.assertEqual(out["html"].count('class="badge mirrored"'), 1)

    def test_the_tag_cannot_push_the_title_off_a_phone(self):
        css = (STATIC / "styles.css").read_text(encoding="utf-8")
        # The title is the shrinking part, the tag a fixed short word, and
        # under 720 px the tag gets tighter.
        self.assertRegex(css, r"\.ellipsis \{[^}]*min-width: 0")
        self.assertRegex(css, r"\.session-title \{[^}]*min-width: 0")
        self.assertRegex(css, r"\.badge \{ flex: none;")
        self.assertRegex(css, r"@media \(max-width: 720px\) \{[^@]*\.session-title \.badge\.mirrored \{[^}]*font-size: 11px")


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TestMirroredChat(unittest.TestCase):
    """pages/chat.js under node with the Queued tray tests' DOM stub."""

    def run_js(self, body):
        source = CHAT_JS.read_text(encoding="utf-8")
        script = (PRELUDE + source + "\nconst T = SessionChat._test;\n"
                  "ids['chat-loading'] = el('p'); ids['chat-loading'].className = 'empty';\n"
                  "T.chat.key = 'claude:s1'; T.chat.gen = 1; T.chat.rowKnown = true;\n"
                  "const banner = () => ({ html: ids['chat-banner'].innerHTML, hidden: ids['chat-banner'].classList.contains('hidden'),\n"
                  "  calm: ids['chat-banner'].classList.contains('calm') });\n"
                  "(async () => {" + body + "})()"
                  ".then((r) => process.stdout.write(JSON.stringify(r)))"
                  ".catch((e) => { console.error(e); process.exit(1); });\n")
        return _run_node(self, script)

    def test_a_mirrored_chat_opens_with_the_one_line_notice_and_nothing_to_press(self):
        out = self.run_js("""
            T.chat.row = { mirrored: true, status: null, attachable: true, ownership: "observed", kept: true };
            T.updateOwnership();
            return { banner: banner(), composerOff: ids['chat-text'].disabled, placeholder: ids['chat-text'].placeholder };""")
        self.assertFalse(out["banner"]["hidden"])
        self.assertTrue(out["banner"]["calm"])
        self.assertIn(HINT, out["banner"]["html"])
        for absent in ("<button", "Attach", "Reattach", "Observed session", "tmux"):
            self.assertNotIn(absent, out["banner"]["html"])
        self.assertTrue(out["composerOff"])
        self.assertIn("Read-only", out["placeholder"])

    def test_the_notice_shows_before_the_catalog_row_lands(self):
        out = self.run_js("""
            T.chat.rowKnown = false;
            T.chat.row = { mirrored: true };
            T.updateOwnership();
            return banner();""")
        self.assertFalse(out["hidden"])
        self.assertIn(HINT, out["html"])

    def test_a_live_observed_chat_is_unchanged(self):
        out = self.run_js("""
            T.chat.row = { status: "done", attachable: true, ownership: "observed" };
            T.updateOwnership();
            return banner();""")
        self.assertFalse(out["calm"])
        self.assertIn("Observed session: read-only until attached.", out["html"])
        self.assertIn('id="chat-attach"', out["html"])
        self.assertNotIn(HINT, out["html"])

    def test_an_unreadable_copy_reads_as_a_calm_empty_state_not_an_error(self):
        out = self.run_js("""
            T.chat.row = { mirrored: true };
            globalThis.api = async () => ({ status: 404, data: null });
            const status = await T.backfill(1);
            return { status, log: ids['chat-loading'].outerHTML, error: (ids['chat-error'] || {}).innerHTML || "" };""")
        self.assertEqual(out["status"], 404)
        self.assertIn("This session&#39;s chat is not available yet.", out["log"])
        self.assertIn("chat-empty-calm", out["log"])
        self.assertNotIn("Couldn", out["log"])
        self.assertNotIn("No transcript", out["log"])
        self.assertEqual(out["error"], "")

    def test_an_empty_copy_and_a_server_error_read_the_same_way(self):
        out = self.run_js("""
            T.chat.row = { mirrored: true };
            globalThis.api = async () => ({ status: 200, data: { messages: [] } });
            await T.backfill(1);
            const empty = ids['chat-loading'].outerHTML;
            ids['chat-loading'] = el('p'); ids['chat-loading'].className = 'empty';
            globalThis.api = async () => ({ status: 500, data: null });
            await T.backfill(1);
            return [empty, ids['chat-loading'].outerHTML];""")
        for html in out:
            self.assertIn("chat is not available yet", html)

    def test_a_chat_opened_from_a_link_settles_once_the_row_says_mirrored(self):
        out = self.run_js("""
            // Unknown at backfill: the usual wording lands first.
            globalThis.api = async () => ({ status: 404, data: null });
            await T.backfill(1);
            const before = ids['chat-loading'].outerHTML;
            // The catalog row arrives afterwards.
            ids['chat-loading'].className = 'empty';
            T.chat.row = { mirrored: true };
            T.updateOwnership();
            return [before, ids['chat-loading'].outerHTML];""")
        self.assertIn("No transcript for this session.", out[0])
        self.assertIn("chat is not available yet", out[1])

    def test_a_live_chat_keeps_its_own_empty_and_error_wording(self):
        out = self.run_js("""
            T.chat.row = { status: "done", ownership: "observed" };
            globalThis.api = async () => ({ status: 404, data: null });
            await T.backfill(1);
            const missing = ids['chat-loading'].outerHTML;
            ids['chat-loading'] = el('p');
            globalThis.api = async () => ({ status: 200, data: { messages: [] } });
            await T.backfill(1);
            return [missing, ids['chat-loading'].outerHTML];""")
        self.assertIn("No transcript for this session.", out[0])
        self.assertIn("No messages yet.", out[1])

    def test_attach_does_nothing_for_a_mirrored_session(self):
        out = self.run_js("""
            T.chat.row = { mirrored: true, attachable: true, ownership: "observed" };
            await T.attach();
            return posts.length;""")
        self.assertEqual(out, 0)


if __name__ == "__main__":
    unittest.main()
