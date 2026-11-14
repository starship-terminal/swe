"""The web ledger and swe's transcript mirror (#1918).

Usage is never lost and never counted twice: events recorded for a
session stay after the harness prunes its transcript, a copy never adds
an event a second time, and a session the ledger never read (or read only
partly) before it was pruned is read from its copy under
``~/.swe/data/transcripts/``. The rule is ``scan._scan_copies``, the port
of ``Ledger.scanCopies`` and ``Ledger.cursor(_:covers:copy:)``
(``LedgerMirrorTests.swift`` pins the Swift side).

Every test runs in a throwaway HOME, with clones off so the streamed copy
path is what runs everywhere.
"""

import json
import os
import shutil
import unittest
from pathlib import Path
from unittest import mock

from swe.ledger.scan import scan
from swe.ledger.store import LedgerStore, ledger_path_for
from swe.paths import transcripts_dir_for
from swe.sessions import transcript_mirror as mirror
from tests._tempdir import temp_dir

CLAUDE_ID = "11111111-2222-3333-4444-555555555555"
OTHER_ID = "22222222-2222-3333-4444-555555555555"
THREAD = "0199aaaa-bbbb-cccc-dddd-eeeeffff0001"
STEM = f"rollout-2026-09-01T10-00-00-{THREAD}"
MTIME = 1_790_000_000


def claude_event(session_id, n, inp=100, out=10, pad=0):
    """One assistant line with usage; ``pad`` grows it by that many bytes
    of text, so a transcript can be made past the fingerprint's reach."""
    return {"type": "assistant", "sessionId": session_id, "cwd": "/work/proj",
            "requestId": f"req_{n:04d}", "timestamp": f"2026-10-01T10:{n % 60:02d}:05.000Z",
            "message": {"id": f"msg_{n:04d}", "model": "claude-opus-4-1",
                        "content": [{"type": "text", "text": "x" * pad}],
                        "usage": {"input_tokens": inp, "output_tokens": out}}}


def codex_lines(*totals):
    out = [{"type": "session_meta", "timestamp": "2026-09-01T10:00:00.000Z",
            "payload": {"id": THREAD, "cwd": "/work/other"}},
           {"type": "turn_context", "payload": {"model": "gpt-5-codex"}}]
    for n, (inp, outp) in enumerate(totals):
        out.append({"type": "event_msg", "timestamp": f"2026-09-01T10:0{n}:00.000Z",
                    "payload": {"type": "token_count", "info": {
                        "last_token_usage": {"input_tokens": inp, "output_tokens": outp},
                        "total_token_usage": {"input_tokens": inp * (n + 1),
                                              "output_tokens": outp * (n + 1)}}}})
    return out


def write_jsonl(path: Path, records, mtime_ns=MTIME * 10**9) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records))
    os.utime(path, ns=(mtime_ns, mtime_ns))
    return path


class LedgerMirrorCase(unittest.TestCase):
    def setUp(self):
        # Not resolved on purpose: on macOS the temp folder sits under
        # /var, a link to /private/var, so the ledger's cursor keys (the
        # walked spelling) and the manifest's sources (realpath) differ,
        # as they do for any home behind a link.
        self.home = temp_dir(self)
        self.store = LedgerStore(ledger_path_for(self.home))
        self.addCleanup(self.store.close)
        patch = mock.patch.object(mirror, "_clone", side_effect=OSError(95, "no clone"))
        patch.start()
        self.addCleanup(patch.stop)

    # -- transcripts -------------------------------------------------------

    def claude(self, events, session_id=CLAUDE_ID, folder="-work-proj", home=None,
               mtime_ns=MTIME * 10**9) -> Path:
        base = home if home is not None else self.home / ".claude"
        return write_jsonl(base / "projects" / folder / f"{session_id}.jsonl",
                           [claude_event(session_id, *e) if isinstance(e, tuple)
                            else claude_event(session_id, e) for e in events], mtime_ns)

    def codex(self, *totals) -> Path:
        return write_jsonl(self.home / ".codex/sessions/2026/09/01" / f"{STEM}.jsonl",
                           codex_lines(*totals))

    # -- actions -----------------------------------------------------------

    def mirror_pass(self):
        result = mirror.run_pass(self.home)
        self.assertEqual(result.failed, 0)
        self.assertGreater(result.copied, 0)
        return result

    def scan(self):
        return scan(self.store, self.home)

    def reference(self, build) -> tuple:
        """The totals a scan of the same transcripts, all live and with no
        mirror, gives: what every test below must land on."""
        home = temp_dir(self)
        store = LedgerStore(ledger_path_for(home))
        self.addCleanup(store.close)
        build(home)
        scan(store, home)
        return totals(store)

    def totals(self) -> tuple:
        return totals(self.store)


def totals(store) -> tuple:
    """``(events, input, output)`` over the whole ledger."""
    n, inp, out = store.db.execute(
        "SELECT COUNT(*), COALESCE(SUM(input), 0), COALESCE(SUM(output), 0) FROM events"
    ).fetchone()
    return n, inp, out


class PrunedSessionTests(LedgerMirrorCase):
    """(a), (b), (c): a pruned session's usage is kept, exactly once."""

    def test_a_session_pruned_before_any_scan_keeps_its_usage(self):
        # (a) Mirrored, then pruned, before the ledger ever saw it.
        source = self.claude([1, 2, 3])
        self.mirror_pass()
        source.unlink()
        first = self.scan()
        self.assertEqual(first.errors, [])
        self.assertEqual(first.files_read, 1)
        self.assertEqual(self.totals(), (3, 300, 30))
        # Read once: its own cursor keeps it from being read again.
        second = self.scan()
        self.assertEqual(second.files_read, 0)
        self.assertEqual(self.totals(), (3, 300, 30))
        # Filed under the harness home, as a live scan files it.
        rows = self.store.db.execute("SELECT DISTINCT account, session FROM events").fetchall()
        self.assertEqual(rows, [(str(self.home / ".claude"), CLAUDE_ID)])

    def test_a_scanned_then_pruned_session_is_not_counted_twice(self):
        # (b) The ledger read it whole, then it was mirrored and pruned. A
        # transcript past head and tail samples' reach: the copy is read
        # (nothing proves it holds the same bytes), and every line it holds
        # upserts onto the row it already has.
        source = self.claude([(n, 100, 10, 600) for n in range(1, 31)])
        self.assertGreater(source.stat().st_size, 2 * 4096)
        self.scan()
        self.assertEqual(self.totals(), (30, 3000, 300))
        self.mirror_pass()
        source.unlink()
        pruned = self.scan()
        self.assertEqual(pruned.errors, [])
        self.assertEqual(pruned.files_read, 1)
        self.assertEqual(self.totals(), (30, 3000, 300))
        self.assertEqual(self.scan().files_read, 0)
        self.assertEqual(self.totals(), (30, 3000, 300))

    def test_a_copy_the_source_cursor_covers_is_not_read(self):
        # (b) A transcript head and tail samples cover end to end: the
        # source's cursor proves it read exactly the copy's bytes.
        source = self.claude([1, 2])
        self.assertLessEqual(source.stat().st_size, 2 * 4096)
        self.scan()
        self.mirror_pass()
        source.unlink()
        pruned = self.scan()
        self.assertEqual(pruned.files_read, 0)
        self.assertEqual(self.totals(), (2, 200, 20))

    def test_a_rewritten_shorter_then_pruned_transcript_keeps_its_new_usage(self):
        # (c) Read, then rewritten by the harness with a new event in a
        # shorter file, mirrored, pruned. The source's cursor reads past
        # the copy's size, which proves nothing about its bytes: the copy
        # is read and its new event counts once.
        source = self.claude([1, 2])
        self.scan()
        old_size = source.stat().st_size
        self.claude([5], mtime_ns=(MTIME + 100) * 10**9)
        self.assertLess(source.stat().st_size, old_size)
        self.mirror_pass()
        source.unlink()
        self.scan()
        self.assertEqual(self.totals(), (3, 300, 30))
        self.scan()
        self.assertEqual(self.totals(), (3, 300, 30))

    def test_a_same_size_rewrite_past_the_samples_is_read(self):
        # The Python cursor has head and tail samples but no middle one, so
        # it proves nothing about a file longer than both together: a
        # rewrite in the middle with the same size and mtime is read.
        events = [(n, 100, 10, 600) for n in range(1, 31)]
        source = self.claude(events)
        self.scan()
        # Swap one event's ids in the middle, keeping every byte count.
        text = source.read_text()
        self.assertIn('"msg_0015"', text)
        text = text.replace('"msg_0015"', '"msg_9915"').replace('"req_0015"', '"req_9915"')
        source.write_text(text)
        os.utime(source, ns=(MTIME * 10**9, MTIME * 10**9))
        self.mirror_pass()
        source.unlink()
        self.scan()
        self.assertEqual(self.totals(), (31, 3100, 310))

    def test_a_grown_then_pruned_transcript_adds_only_its_new_events(self):
        source = self.claude([1])
        self.scan()
        self.claude([1, 2], mtime_ns=(MTIME + 100) * 10**9)
        self.mirror_pass()
        source.unlink()
        self.scan()
        self.assertEqual(self.totals(), (2, 200, 20))

    def test_a_pruned_codex_rollout_is_read_once_from_its_copy(self):
        expected = self.reference(lambda home: write_jsonl(
            home / ".codex/sessions/2026/09/01" / f"{STEM}.jsonl", codex_lines((7, 3), (9, 4))))
        self.assertEqual(expected[0], 2)
        source = self.codex((7, 3), (9, 4))
        self.mirror_pass()
        source.unlink()
        self.assertEqual(self.scan().files_read, 1)
        self.assertEqual(self.totals(), expected)
        self.assertEqual(self.scan().files_read, 0)
        self.assertEqual(self.totals(), expected)
        sessions = self.store.db.execute("SELECT DISTINCT harness, account, session FROM events")
        self.assertEqual(sessions.fetchall(), [("codex", str(self.home / ".codex"), STEM)])

    def test_a_scanned_then_pruned_codex_rollout_is_not_counted_twice(self):
        source = self.codex((7, 3), (9, 4))
        self.scan()
        before = self.totals()
        self.mirror_pass()
        source.unlink()
        self.scan()
        self.assertEqual(self.totals(), before)

    def test_a_lost_manifest_still_finds_the_copy_and_counts_it_once(self):
        source = self.claude([1, 2])
        self.mirror_pass()
        source.unlink()
        mirror.manifest_path(self.home).unlink()
        self.scan()
        self.scan()
        self.assertEqual(self.totals(), (2, 200, 20))

    def test_an_account_copy_is_filed_under_its_account_home(self):
        account = self.home / ".swe/accounts/claude/work/home"
        source = self.claude([1], home=account)
        self.mirror_pass()
        source.unlink()
        self.scan()
        rows = self.store.db.execute("SELECT DISTINCT account FROM events").fetchall()
        self.assertEqual(rows, [(str(account),)])


class LiveSessionTests(LedgerMirrorCase):
    """(d): a copy never stands for a session a live file carries."""

    def test_a_copy_of_a_session_still_live_elsewhere_is_not_read(self):
        # The same session at two paths: one live, one pruned. The pruned
        # one's copy holds an event of its own, which would count if read.
        self.claude([1], folder="-live")
        gone = self.claude([1, 2], folder="-gone", mtime_ns=(MTIME + 100) * 10**9)
        self.mirror_pass()
        gone.unlink()
        self.scan()
        self.assertEqual(self.totals(), (1, 100, 10))

    def test_a_live_session_is_read_from_its_source_not_its_copy(self):
        self.claude([1, 2])
        self.mirror_pass()
        result = self.scan()
        self.assertEqual(result.files_read, 1)
        keys = [r[0] for r in self.store.db.execute("SELECT path FROM files")]
        self.assertFalse([k for k in keys if k.startswith(
            os.path.realpath(transcripts_dir_for(self.home)))])
        self.assertEqual(self.totals(), (2, 200, 20))


class LinkedMirrorTests(LedgerMirrorCase):
    """(e): a linked tree inside the mirror reads as absent, as search and
    the listing read it, so a link never files copies twice."""

    def _link_tree(self, rel: str) -> None:
        tree = transcripts_dir_for(self.home) / rel
        elsewhere = temp_dir(self) / "tree"
        shutil.move(str(tree), str(elsewhere))
        tree.symlink_to(elsewhere, target_is_directory=True)

    def test_a_linked_harness_tree_is_not_read(self):
        source = self.claude([1, 2])
        self.mirror_pass()
        source.unlink()
        self._link_tree("claude")
        result = self.scan()
        self.assertEqual(result.errors, [])
        self.assertEqual(self.totals(), (0, 0, 0))

    def test_a_linked_account_tree_is_not_read(self):
        source = self.claude([1], home=self.home / ".swe/accounts/claude/work/home")
        self.mirror_pass()
        source.unlink()
        self._link_tree("accounts/claude/work")
        self.scan()
        self.assertEqual(self.totals(), (0, 0, 0))


class FailedReadTests(LedgerMirrorCase):
    """(f): a copy that cannot be read is a scan note, and is retried."""

    @unittest.skipIf(os.geteuid() == 0, "root reads anything")
    def test_an_unreadable_copy_is_noted_and_read_once_readable(self):
        source = self.claude([1, 2])
        self.mirror_pass()
        source.unlink()
        copy = transcripts_dir_for(self.home) / "claude/-work-proj" / f"{CLAUDE_ID}.jsonl"
        self.assertTrue(copy.is_file())
        copy.chmod(0)
        self.addCleanup(copy.chmod, 0o600)
        result = self.scan()
        self.assertEqual(len(result.errors), 1)
        self.assertIn(f"{CLAUDE_ID}.jsonl", result.errors[0])
        self.assertEqual(self.totals(), (0, 0, 0))
        copy.chmod(0o600)
        result = self.scan()
        self.assertEqual(result.errors, [])
        self.assertEqual(self.totals(), (2, 200, 20))


if __name__ == "__main__":
    unittest.main()
