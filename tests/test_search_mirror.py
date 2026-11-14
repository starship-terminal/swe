"""Search over transcript mirror copies (#1918).

A Claude Code or Codex session whose transcript the harness pruned stays
searchable through the copy ``~/.swe/data/transcripts/`` holds, including
text the index never read before the source went. The rule lives in
``search_writer._index_copies``; each class below pins one invariant of it:

1. ``LiveSessionsTests``: never worse than no mirror. Sessions whose source
   still exists search exactly as they do with no mirror, in every mirror
   state.
2. ``OneSessionOneRowTests``: a session is never counted or returned twice.
3. ``NothingLostTests``: losing or corrupting the manifest removes no row
   and no copy; a row is replaced only from a copy at least as complete.
4. ``NewerWinsTests``: a copy that holds replies the index lacks wins.
5. ``SharedIndexTests``: no DDL or schema version change, and a writer
   that knows nothing about the mirror (the Swift app today) never undoes
   these rows or indexes the session a second time.
6. ``CostTests``: an unchanged copy is not read again, and no directory is
   listed per session.

Every test runs in a throwaway HOME, with clones off so the streamed copy
path is what runs everywhere.
"""

import functools
import json
import os
import shutil
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from swe.paths import transcripts_dir_for
from swe.sessions import search_index
from swe.sessions import search_writer as writer
from swe.sessions import transcript_mirror as mirror
from tests._tempdir import temp_dir

CLAUDE_ID = "11111111-2222-3333-4444-555555555555"
OTHER_ID = "22222222-2222-3333-4444-555555555555"
GONE_ID = "33333333-2222-3333-4444-555555555555"
THREAD = "0199aaaa-bbbb-cccc-dddd-eeeeffff0001"
STEM = f"rollout-2026-09-01T10-00-00-{THREAD}"
LATER_STEM = f"rollout-2026-09-03T10-00-00-{THREAD}"
MTIME = 1_790_000_000
INDEXER = writer.Indexer  # the class, before small_buffer swaps in a partial


def claude_lines(session_id, *prompts, start=0):
    out = []
    for n, prompt in enumerate(prompts, start):
        out.append({"type": "user", "sessionId": session_id, "cwd": "/work/proj",
                    "timestamp": f"2026-10-01T10:{n:02d}:00.000Z",
                    "message": {"role": "user", "content": prompt}})
        out.append({"type": "assistant", "sessionId": session_id,
                    "timestamp": f"2026-10-01T10:{n:02d}:05.000Z",
                    "message": {"content": [{"type": "text", "text": f"done {n}"}],
                                "stop_reason": "end_turn"}})
    return out


def codex_lines(thread, *prompts):
    out = [{"type": "session_meta", "payload": {"id": thread, "cwd": "/work/other"}}]
    for prompt in prompts:
        out.append({"type": "response_item", "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": prompt}]}})
    return out


def write_jsonl(path: Path, records, mtime=MTIME, mode="w") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode) as handle:
        handle.write("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records))
    os.utime(path, (mtime, mtime))
    return path


class MirrorSearchCase(unittest.TestCase):
    def setUp(self):
        self.home = temp_dir(self).resolve()
        self.mirror_root = transcripts_dir_for(self.home)
        patch = mock.patch.object(mirror, "_clone", side_effect=OSError(95, "no clone"))
        patch.start()
        self.addCleanup(patch.stop)

    # -- transcripts -------------------------------------------------------

    def claude(self, session_id=CLAUDE_ID, *prompts, folder="-work-proj", mtime=MTIME) -> Path:
        return write_jsonl(self.home / ".claude/projects" / folder / f"{session_id}.jsonl",
                           claude_lines(session_id, *(prompts or ("fix the login bug",))), mtime)

    def grow(self, path: Path, session_id, *prompts, mtime) -> Path:
        return write_jsonl(path, claude_lines(session_id, *prompts, start=10), mtime, mode="a")

    def codex(self, stem=STEM, *prompts, under="sessions/2026/09/01", mtime=MTIME,
              thread=THREAD) -> Path:
        return write_jsonl(self.home / ".codex" / under / f"{stem}.jsonl",
                           codex_lines(thread, *(prompts or ("add dark mode",))), mtime)

    # -- actions -----------------------------------------------------------

    def mirror_only(self):
        """A mirror pass with no index pass, as ``swe session mirror`` or
        the hub with search indexing off runs it."""
        return mirror.run_pass(self.home)

    def index(self):
        return writer.run_pass(self.home)

    def index_live_only(self):
        """What a writer that knows nothing about the mirror does: index
        the transcripts the harness roots hold, and nothing else. This is
        the Swift writer's behaviour until it is ported."""
        conn = writer.open_index(search_index.index_path(self.home))
        try:
            indexer = writer.Indexer(writer.Store(conn))
            for source in writer.sources(self.home):
                indexer.index_file(source.path, source.harness, document=source.document,
                                   session_id=source.session_id, meta_path=source.meta_path,
                                   pair=source.pair)
        finally:
            conn.close()

    # -- what the index holds ----------------------------------------------

    def db(self):
        conn = sqlite3.connect(search_index.index_path(self.home))
        self.addCleanup(conn.close)
        return conn

    def files(self) -> list[tuple]:
        return self.db().execute(
            "SELECT f.path, f.ino, f.byte_offset, f.mtime_ms, f.size_bytes, f.state,"
            " s.agent, s.session_id, s.file_path, s.title, s.message_count"
            " FROM files f LEFT JOIN sessions s ON s.id = f.session_row_id"
            " ORDER BY f.path").fetchall()

    def session_rows(self, agent=None) -> list[tuple]:
        sql = "SELECT agent, session_id, file_path, message_count FROM sessions"
        if agent:
            return self.db().execute(sql + " WHERE agent = ? ORDER BY id", (agent,)).fetchall()
        return self.db().execute(sql + " ORDER BY id").fetchall()

    def hits(self, word) -> list[str]:
        return [h.session_id for h in search_index.search(self.home, word)]

    def messages_text(self) -> list[tuple]:
        """Every live message's text, with the path it is filed under."""
        return self.db().execute(
            "SELECT s.file_path, f.user_text, f.assistant_text FROM messages m"
            " JOIN sessions s ON s.id = m.session_row_id"
            " JOIN messages_fts f ON f.rowid = m.id ORDER BY s.file_path, m.id").fetchall()

    def copies(self) -> list[tuple]:
        return sorted((str(p.relative_to(self.mirror_root)), p.read_bytes())
                      for p in self.mirror_root.rglob("*") if p.is_file()
                      and not p.name.startswith(("manifest.json", "mirror.json"))
                      and not p.name.endswith(".lock"))

    def lose_manifest(self, how):
        path = mirror.manifest_path(self.home)
        if how == "missing":
            path.unlink()
        else:
            path.write_text("{not json")


class GoalTests(MirrorSearchCase):
    def test_a_session_the_index_never_read_is_found_through_its_copy_once(self):
        source = self.claude(CLAUDE_ID, "quokka migration plan")
        self.mirror_only()
        source.unlink()
        self.index()
        self.assertEqual(self.hits("quokka"), [CLAUDE_ID])
        # Filed under the transcript's own path, the key both writers use,
        # never under the copy's.
        self.assertEqual(self.session_rows(), [("claude", CLAUDE_ID, str(source), 2)])
        self.assertTrue(all(str(self.mirror_root) not in row[0] for row in self.files()))

    def test_a_pruned_codex_rollout_is_found_through_its_copy(self):
        source = self.codex(STEM, "wombat dashboards")
        self.mirror_only()
        source.unlink()
        self.index()
        self.assertEqual(len(self.hits("wombat")), 1)
        self.assertEqual([r[2] for r in self.session_rows("codex")], [str(source)])

    def test_the_cli_search_finds_it_once(self):
        from tests.test_json_output import decode, run_swe

        # The word is only in replies the index never read, never in the
        # title, so a title match cannot pass this.
        source = self.claude(CLAUDE_ID, "first question")
        self.index()
        self.grow(source, CLAUDE_ID, "then the narwhal rollout", mtime=MTIME + 100)
        self.mirror_only()
        source.unlink()
        self.index()
        result = run_swe(self.home, "session", "--search=narwhal", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = decode(result)["data"]["sessions"]
        self.assertEqual([r["id"] for r in rows], [CLAUDE_ID])
        self.assertTrue(rows[0]["mirrored"])


class NewerWinsTests(MirrorSearchCase):
    def test_replies_saved_only_in_the_copy_become_searchable(self):
        # Indexed, grown while indexing was off, mirrored, then pruned:
        # the review's "newer replies stay unsearchable" case.
        source = self.claude(CLAUDE_ID, "first question")
        self.index()
        self.grow(source, CLAUDE_ID, "pelican follow up", mtime=MTIME + 100)
        self.mirror_only()
        source.unlink()
        self.index()
        self.assertEqual(self.hits("pelican"), [CLAUDE_ID])
        self.assertEqual(self.hits("first"), [CLAUDE_ID])
        self.assertEqual(self.session_rows(), [("claude", CLAUDE_ID, str(source), 4)])

    def test_the_newest_copy_of_a_session_in_two_folders_wins(self):
        # Resumed from another project folder: one id, two files, the
        # newer holding the whole conversation. Only the older was indexed.
        older = self.claude(CLAUDE_ID, "first question", folder="-work-a")
        self.index()
        newer = write_jsonl(self.home / ".claude/projects/-work-b" / f"{CLAUDE_ID}.jsonl",
                            claude_lines(CLAUDE_ID, "first question", "toucan reply"),
                            MTIME + 100)
        self.mirror_only()
        older.unlink(), newer.unlink()
        self.index()
        self.assertEqual(self.hits("toucan"), [CLAUDE_ID])
        self.assertEqual(self.session_rows(), [("claude", CLAUDE_ID, str(newer), 4)])

    def test_a_newer_rollout_of_a_codex_thread_wins(self):
        first = self.codex(STEM, "add dark mode")
        self.index()
        later = self.codex(LATER_STEM, "add dark mode", "heron contrast", mtime=MTIME + 100)
        self.mirror_only()
        first.unlink(), later.unlink()
        self.index()
        self.assertEqual(len(self.hits("heron")), 1)
        self.assertEqual([r[2] for r in self.session_rows("codex")], [str(later)])


class OneSessionOneRowTests(MirrorSearchCase):
    def assert_one(self, agent, word):
        self.assertEqual(len(self.session_rows(agent)), 1, self.session_rows(agent))
        self.assertEqual(len(self.hits(word)), 1)
        self.assertEqual(writer.status(self.home)["sessions_by_agent"].get(agent), 1)

    def test_a_rollout_mirrored_then_archived_before_any_index_pass(self):
        # The review's "archived rollouts crowd search": the copy's source
        # path is gone, but the thread is live at its archived path.
        dated = self.codex(STEM, "ibis thread")
        self.mirror_only()
        dated.unlink()
        self.codex(STEM, "ibis thread", under="archived_sessions", mtime=MTIME + 5)
        self.index()
        self.assert_one("codex", "ibis")

    def test_a_dated_archived_layout(self):
        dated = self.codex(STEM, "ibis thread")
        self.mirror_only()
        dated.unlink()
        self.codex(STEM, "ibis thread", under="archived_sessions/2026/09/01", mtime=MTIME + 5)
        self.mirror_only()
        self.index()
        self.assert_one("codex", "ibis")

    def test_a_thread_with_several_pruned_rollouts(self):
        first = self.codex(STEM, "ibis thread")
        later = self.codex(LATER_STEM, "ibis thread", "and more", mtime=MTIME + 100)
        self.mirror_only()
        first.unlink(), later.unlink()
        self.index()
        self.assert_one("codex", "ibis")
        self.index()
        self.assert_one("codex", "ibis")

    def test_a_thread_whose_older_rollout_was_indexed_and_newer_was_not(self):
        first = self.codex(STEM, "ibis thread")
        self.index()
        later = self.codex(LATER_STEM, "ibis thread", "and more", mtime=MTIME + 100)
        self.mirror_only()
        first.unlink(), later.unlink()
        self.index()
        self.assert_one("codex", "ibis")

    def test_a_claude_session_in_two_folders_both_pruned(self):
        a = self.claude(CLAUDE_ID, "egret work", folder="-work-a")
        b = self.claude(CLAUDE_ID, "egret work", "more", folder="-work-b", mtime=MTIME + 50)
        self.mirror_only()
        a.unlink(), b.unlink()
        self.index()
        self.assert_one("claude", "egret")

    def test_a_claude_session_whose_other_folder_is_still_live(self):
        a = self.claude(CLAUDE_ID, "egret work", folder="-work-a")
        self.claude(CLAUDE_ID, "egret work", "more", folder="-work-b", mtime=MTIME + 50)
        self.mirror_only()
        a.unlink()
        self.index()
        self.assert_one("claude", "egret")

    def test_every_pass_after_keeps_one_row(self):
        source = self.claude(CLAUDE_ID, "egret work")
        self.index()
        self.mirror_only()
        source.unlink()
        for _ in range(3):
            self.index()
            self.assert_one("claude", "egret")


class LiveSessionsTests(MirrorSearchCase):
    """For sessions whose source exists, the index with a mirror in any
    state equals the index with no mirror at all."""

    QUERIES = ("login", "egret", "ibis", "zebra", "stale", "done")

    def seed(self):
        self.claude(CLAUDE_ID, "fix the login bug")
        self.claude(OTHER_ID, "egret work", folder="-work-b", mtime=MTIME + 3)
        self.codex(STEM, "ibis thread")
        self.codex(LATER_STEM, "ibis thread", "and more", under="archived_sessions",
                   mtime=MTIME + 9)

    def view(self, live: set[str]):
        """Everything filed for a live session, under any path: its rows,
        text and hits. A row a copy put in for a live session under some
        other path shows here too."""
        ids = {(h, writer.transcript_identity(h, p)) for p in live
               for h in ("claude", "codex") if f"/.{h}/" in p}

        def mine(path):
            return any((h, writer.transcript_identity(h, path)) in ids for h in ("claude", "codex"))
        files = [r for r in self.files() if mine(r[0])]
        text = [r for r in self.messages_text() if mine(r[0])]
        hits = {q: sorted((h.session_id, h.file_path, h.snippet)
                          for h in search_index.search(self.home, q) if mine(h.file_path))
                for q in self.QUERIES}
        return files, text, hits

    def test_every_mirror_state_leaves_live_sessions_as_they_are_without_a_mirror(self):
        self.seed()
        live = {s.path for s in writer.sources(self.home)}
        mirror.set_enabled(self.home, False)
        self.index()
        baseline = self.view(live)
        self.assertTrue(baseline[0] and baseline[1])
        shutil.rmtree(self.mirror_root)  # no mirror at all, not even its switch

        # A pruned session beside the live ones, so the copy step has work.
        gone = self.claude(GONE_ID, "gone work", folder="-work-c")
        mirror.run_pass(self.home)
        gone.unlink()

        states = []
        self.index()
        states.append(("normal", self.view(live)))
        self.lose_manifest("missing")
        self.index()
        states.append(("manifest missing", self.view(live)))
        self.lose_manifest("corrupt")
        self.index()
        states.append(("manifest corrupt", self.view(live)))
        # Stale copies of live ids, older and smaller than their sources.
        write_jsonl(self.mirror_root / "claude/-work-proj" / f"{CLAUDE_ID}.jsonl",
                    claude_lines(CLAUDE_ID, "stale words"), MTIME - 100)
        write_jsonl(self.mirror_root / "codex/2026/09/01" / f"{STEM}.jsonl",
                    codex_lines(THREAD, "stale words"), MTIME - 100)
        self.index()
        states.append(("stale copies", self.view(live)))
        # Copies newer and longer than their sources, in their own folders
        # and in folders no source has.
        for folder in ("-work-proj", "-elsewhere"):
            write_jsonl(self.mirror_root / "claude" / folder / f"{CLAUDE_ID}.jsonl",
                        claude_lines(CLAUDE_ID, "fix the login bug", "zebra newer"), MTIME + 900)
        write_jsonl(self.mirror_root / "codex" / f"{STEM}.jsonl",
                    codex_lines(THREAD, "ibis thread", "zebra newer"), MTIME + 900)
        write_jsonl(self.mirror_root / "codex/2026/09/03" / f"{LATER_STEM}.jsonl",
                    codex_lines(THREAD, "ibis thread", "zebra newer"), MTIME + 900)
        self.index()
        states.append(("newer copies", self.view(live)))
        self.lose_manifest("missing")
        self.index()
        states.append(("newer copies, manifest missing", self.view(live)))
        for name, view in states:
            with self.subTest(state=name):
                self.assertEqual(view, baseline)
        self.assertEqual(self.hits("zebra"), [])
        # The pruned session is still found; only live ones are pinned.
        self.assertEqual(self.hits("gone"), [GONE_ID])


class NothingLostTests(MirrorSearchCase):
    def seed_indexed_and_pruned(self):
        """One session indexed live then pruned, one only ever read from
        its copy, one Codex rollout indexed and pruned."""
        a = self.claude(CLAUDE_ID, "kestrel work")
        c = self.codex(STEM, "kestrel codex")
        self.index()
        b = self.claude(OTHER_ID, "kestrel copy only", folder="-work-b")
        self.mirror_only()
        for path in (a, b, c):
            path.unlink()
        self.index()

    def test_losing_or_corrupting_the_manifest_removes_no_row_and_no_copy(self):
        # The review's "manifest loss erases search history".
        self.seed_indexed_and_pruned()
        rows, text, copies = self.files(), self.messages_text(), self.copies()
        self.assertEqual(len(rows), 3)
        for how in ("corrupt", "missing", "corrupt", "missing"):
            self.lose_manifest(how)
            self.index()
            with self.subTest(how=how):
                self.assertEqual(self.files(), rows)
                self.assertEqual(self.messages_text(), text)
                self.assertEqual(self.copies(), copies)
        self.assertEqual(sorted(self.hits("kestrel")), sorted([CLAUDE_ID, OTHER_ID, THREAD]))

    def test_a_copy_found_without_its_manifest_entry_is_still_searchable(self):
        source = self.claude(CLAUDE_ID, "plover notes")
        codex = self.codex(STEM, "plover codex")
        archived = self.codex(LATER_STEM.replace(THREAD, OTHER_ID), "plover archived",
                              under="archived_sessions", thread=OTHER_ID)
        self.mirror_only()
        for path in (source, codex, archived):
            path.unlink()
        self.lose_manifest("missing")
        self.index()
        self.assertEqual(len(self.hits("plover")), 3)
        # Filed under the paths the copies came from.
        self.assertEqual(sorted(r[2] for r in self.session_rows()),
                         sorted(map(str, (source, codex, archived))))

    def test_an_older_copy_never_replaces_a_newer_row(self):
        source = self.claude(CLAUDE_ID, "first question")
        self.mirror_only()
        mirror.set_enabled(self.home, False)
        self.grow(source, CLAUDE_ID, "avocet later", mtime=MTIME + 100)
        self.index()  # reads the grown source; the copy stays old
        before = self.files()
        source.unlink()
        self.index()
        self.assertEqual(self.files(), before)
        self.assertEqual(self.hits("avocet"), [CLAUDE_ID])

    def test_a_shorter_copy_never_replaces_a_longer_row(self):
        source = self.claude(CLAUDE_ID, "first question", "curlew detail")
        self.index()
        # The source is rewritten shorter, mirrored (the longer copy is
        # kept aside as .shrunk-*), then pruned.
        write_jsonl(source, claude_lines(CLAUDE_ID, "first question"), MTIME + 100)
        self.mirror_only()
        before = self.files()
        source.unlink()
        self.index()
        self.assertEqual(self.files(), before)
        self.assertEqual(self.hits("curlew"), [CLAUDE_ID])

    def test_rows_dropped_for_a_newer_copy_only_when_it_holds_as_much(self):
        # Two folders, the newer file shorter than the indexed older one:
        # neither can stand for both, so nothing changes.
        older = self.claude(CLAUDE_ID, "first question", "long tail of words", "more",
                            folder="-work-a")
        self.index()
        before = self.files()
        newer = self.claude(CLAUDE_ID, "short", folder="-work-b", mtime=MTIME + 100)
        self.mirror_only()
        older.unlink(), newer.unlink()
        self.index()
        self.assertEqual(self.files(), before)

    def test_two_files_named_without_a_session_id_are_never_taken_for_one(self):
        # Same name, no uuid in it, two folders: two transcripts, not one
        # session in two places, so neither may stand for the other.
        first = write_jsonl(self.home / ".codex/sessions/2026/09/01/notes.jsonl",
                            codex_lines("", "redshank one"))
        self.index()
        second = write_jsonl(self.home / ".codex/sessions/2026/09/02/notes.jsonl",
                             codex_lines("", "redshank one", "redshank two"), MTIME + 100)
        self.mirror_only()
        first.unlink(), second.unlink()
        self.index()
        self.assertEqual(sorted(r[2] for r in self.session_rows("codex")),
                         sorted([str(first), str(second)]))

    def test_a_copy_with_no_known_source_is_left_alone(self):
        # An external symlinked transcript's copy, its manifest lost: no
        # path to file it under, so it is not indexed and nothing changes.
        elsewhere = temp_dir(self) / f"{CLAUDE_ID}.jsonl"
        write_jsonl(elsewhere, claude_lines(CLAUDE_ID, "dunlin"))
        link = self.home / ".claude/projects/-work-proj" / elsewhere.name
        link.parent.mkdir(parents=True)
        link.symlink_to(elsewhere)
        self.mirror_only()
        link.unlink(), elsewhere.unlink()
        self.lose_manifest("missing")
        self.index()
        self.assertEqual(self.hits("dunlin"), [])
        self.assertEqual(len(self.copies()), 1)


class SharedIndexTests(MirrorSearchCase):
    def test_the_schema_and_version_are_unchanged(self):
        source = self.claude(CLAUDE_ID, "sandpiper")
        self.mirror_only()
        source.unlink()
        self.index()
        conn = self.db()
        self.assertEqual(conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'").fetchone(), ("1",))
        fresh = sqlite3.connect(":memory:")
        fresh.executescript(writer.TABLES_SQL)
        fresh.executescript(writer.FTS_SQL)

        def ddl(c):
            return sorted(r for r in c.execute(
                "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
        self.assertEqual(ddl(conn), ddl(fresh))
        fresh.close()

    def test_a_writer_that_knows_nothing_of_the_mirror_keeps_the_rows(self):
        source = self.claude(CLAUDE_ID, "sandpiper")
        self.mirror_only()
        source.unlink()
        self.index()
        rows = self.files()
        self.index_live_only()
        self.assertEqual(self.files(), rows)

    def test_a_restored_transcript_is_not_indexed_twice(self):
        # The copy goes back where it came from (swe session restore, or a
        # backup): the mirror-blind writer reads it as current.
        source = self.claude(CLAUDE_ID, "sandpiper")
        self.mirror_only()
        copy = self.mirror_root / "claude/-work-proj" / source.name
        source.unlink()
        self.index()
        shutil.copy2(copy, source)
        self.index_live_only()
        self.assertEqual(len(self.session_rows()), 1)
        self.index()
        self.assertEqual(len(self.session_rows()), 1)
        self.assertEqual(self.hits("sandpiper"), [CLAUDE_ID])

    def test_a_restored_transcript_that_grew_is_replaced_in_place(self):
        source = self.claude(CLAUDE_ID, "sandpiper")
        self.mirror_only()
        source.unlink()
        self.index()
        self.claude(CLAUDE_ID, "sandpiper", "stilt", mtime=MTIME + 100)
        self.index_live_only()
        self.assertEqual(self.session_rows(), [("claude", CLAUDE_ID, str(source), 4)])
        self.assertEqual(self.hits("stilt"), [CLAUDE_ID])


class ForgetTests(MirrorSearchCase):
    def test_a_purged_session_stops_being_searchable_and_live_rows_stay(self):
        gone = self.claude(CLAUDE_ID, "bittern notes")
        self.claude(OTHER_ID, "bittern live", folder="-work-b")
        codex = self.codex(STEM, "bittern codex")
        self.mirror_only()
        gone.unlink(), codex.unlink()
        self.index()
        self.assertEqual(len(self.hits("bittern")), 3)
        # What a purge does: the copies first, then the rows they put in.
        for copy in [p for p in self.mirror_root.rglob("*.jsonl")
                     if CLAUDE_ID in p.name or THREAD in p.name]:
            copy.unlink()
        self.assertEqual(writer.forget_session(self.home, "claude", CLAUDE_ID), 1)
        self.assertEqual(writer.forget_session(self.home, "codex", STEM), 1)
        self.assertEqual(writer.forget_session(self.home, "claude", OTHER_ID), 0)
        self.assertEqual(self.hits("bittern"), [OTHER_ID])
        self.index()
        self.assertEqual(self.hits("bittern"), [OTHER_ID])

    def test_it_never_creates_an_index(self):
        self.assertEqual(writer.forget_session(self.home, "claude", CLAUDE_ID), 0)
        self.assertFalse(search_index.index_path(self.home).exists())


class _Boundaries:
    """Records what a reader sees after every commit a pass makes: the
    session rows of one identity and whether a word is still found."""

    COMMITS = ("commit_file", "stage_messages", "retire", "remove_rows", "remove_file")

    def __init__(self, case, word, agent="claude"):
        self.case, self.word, self.agent, self.seen = case, word, agent, []
        self.patches = []
        for name in self.COMMITS:
            real = getattr(writer.Store, name, None)
            if real is None:
                continue
            self.patches.append(mock.patch.object(writer.Store, name, self._wrap(real)))

    def _wrap(self, real):
        def call(store, *args, **kwargs):
            out = real(store, *args, **kwargs)
            self.look()
            return out
        return call

    def look(self):
        conn = sqlite3.connect(search_index.index_path(self.case.home))
        try:
            count = conn.execute("SELECT count(*) FROM sessions WHERE agent = ?",
                                 (self.agent,)).fetchone()[0]
        finally:
            conn.close()
        self.seen.append((count, bool(search_index.search(self.case.home, self.word))))

    def __enter__(self):
        for patch in self.patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in self.patches:
            patch.stop()


def small_buffer(budget=60):
    """Passes whose whole reads commit every ``budget`` characters, as a
    transcript of hundreds of MB does at the real 32 MiB budget."""
    return mock.patch.object(writer, "Indexer", functools.partial(writer.Indexer,
                                                                  buffer_budget=budget))


class StagedReadTests(MirrorSearchCase):
    """A copy read in many commits never shows a second session or a
    prefix in place of a complete row, at any commit boundary or after
    the read dies (review thread 4574590429)."""

    # The old row's last words: a prefix in its place would lose them.
    PROMPTS = tuple(f"kestrel question {n} " + "x" * 40 for n in range(11)) + (
        "kittiwake last old words",)

    def indexed_then_grown_and_pruned(self):
        source = self.claude(CLAUDE_ID, *self.PROMPTS)
        self.index()
        self.grow(source, CLAUDE_ID, *(f"pelican reply {n} " + "y" * 40 for n in range(12)),
                  mtime=MTIME + 100)
        self.mirror_only()
        source.unlink()
        return source

    def live_messages(self) -> int:
        return self.db().execute(
            "SELECT count(*) FROM messages m JOIN sessions s ON s.id = m.session_row_id"
        ).fetchone()[0]

    def test_replacing_a_complete_row_shows_one_complete_session_at_every_commit(self):
        self.indexed_then_grown_and_pruned()
        with small_buffer(), _Boundaries(self, "kittiwake") as seen:
            self.index()
        self.assertGreater(len(seen.seen), 2)  # it really committed in pieces
        self.assertTrue(all(state == (1, True) for state in seen.seen), seen.seen)
        self.assertEqual(self.hits("pelican"), [CLAUDE_ID])
        self.assertEqual(self.session_rows()[0][3], 48)
        self.assertEqual(self.live_messages(), 48)

    def test_a_newer_copy_in_another_folder_never_shows_beside_the_old_row(self):
        older = self.claude(CLAUDE_ID, *self.PROMPTS, folder="-work-a")
        self.index()
        newer = write_jsonl(self.home / ".claude/projects/-work-b" / f"{CLAUDE_ID}.jsonl",
                            claude_lines(CLAUDE_ID, *self.PROMPTS, "pelican tail"), MTIME + 100)
        self.mirror_only()
        older.unlink(), newer.unlink()
        with small_buffer(), _Boundaries(self, "kittiwake") as seen:
            self.index()
        self.assertGreater(len(seen.seen), 2)
        self.assertTrue(all(state == (1, True) for state in seen.seen), seen.seen)
        self.assertEqual([r[2] for r in self.session_rows()], [str(newer)])

    def test_a_read_that_dies_midway_leaves_the_old_row_as_it_was(self):
        self.indexed_then_grown_and_pruned()
        before, text = self.files(), self.messages_text()
        real = INDEXER._flush
        calls = []

        def dying(indexer, pending, stat):
            calls.append(1)
            if len(calls) == 3:
                raise KeyboardInterrupt  # the hub stopped mid-file
            return real(indexer, pending, stat)
        with small_buffer(), mock.patch.object(INDEXER, "_flush", dying):
            with self.assertRaises(KeyboardInterrupt):
                self.index()
        self.assertEqual(self.files(), before)
        self.assertEqual(self.messages_text(), text)
        # The next pass finishes it, with no message read twice.
        with small_buffer():
            self.index()
        self.assertEqual(self.hits("pelican"), [CLAUDE_ID])
        self.assertEqual(self.live_messages(), 48)

    def test_a_source_restored_mid_read_stops_the_read(self):
        # The transcript comes back while its copy is being read: the copy
        # no longer speaks for it, so nothing the read did may land.
        source = self.indexed_then_grown_and_pruned()
        copy = self.mirror_root / "claude/-work-proj" / source.name
        before, text = self.files(), self.messages_text()
        real = INDEXER._flush
        calls = []

        def restoring(indexer, pending, stat):
            calls.append(1)
            if len(calls) == 2:
                shutil.copy2(copy, source)
            return real(indexer, pending, stat)
        with small_buffer(), mock.patch.object(INDEXER, "_flush", restoring), \
                mock.patch.object(writer, "sources", return_value=[]):
            self.index()
        self.assertEqual(self.files(), before)
        self.assertEqual(self.messages_text(), text)

    def test_pieces_left_by_a_killed_pass_are_never_counted(self):
        # A kill gives no chance to clean up: the staged pieces stay, and
        # the next whole read must not count them.
        self.indexed_then_grown_and_pruned()
        real = INDEXER._flush
        calls = []

        def killed(indexer, pending, stat):
            calls.append(1)
            if len(calls) == 3:
                raise SystemExit
            return real(indexer, pending, stat)
        with small_buffer(), mock.patch.object(INDEXER, "_flush", killed), \
                mock.patch.object(writer.Store, "discard_staged", create=True,
                                  side_effect=lambda *a: None):
            with self.assertRaises(SystemExit):
                self.index()
        self.assertEqual(len(self.session_rows()), 1)
        with small_buffer():
            self.index()
        self.assertEqual(self.session_rows()[0][3], 48)
        self.assertEqual(self.live_messages(), 48)


class MovedSessionTests(MirrorSearchCase):
    """A session read from a copy at one path that comes back live at
    another is counted once (review thread 4574590435)."""

    def copy_row_at_a(self):
        a = self.claude(CLAUDE_ID, "first question", folder="-work-a")
        self.mirror_only()
        a.unlink()
        self.index()
        self.assertEqual([r[2] for r in self.session_rows()], [str(a)])
        return a

    def resumed_at_b(self):
        return write_jsonl(self.home / ".claude/projects/-work-b" / f"{CLAUDE_ID}.jsonl",
                           claude_lines(CLAUDE_ID, "first question", "plover resumed"),
                           MTIME + 100)

    def test_a_live_read_retires_the_copy_row_in_the_same_commit(self):
        self.copy_row_at_a()
        b = self.resumed_at_b()
        with _Boundaries(self, "first") as seen:
            self.index()
        self.assertTrue(seen.seen)
        self.assertTrue(all(state == (1, True) for state in seen.seen), seen.seen)
        self.assertEqual([r[2] for r in self.session_rows()], [str(b)])

    def test_a_row_the_mirror_blind_writer_added_retires_the_copy_row_next_pass(self):
        self.copy_row_at_a()
        b = self.resumed_at_b()
        self.index_live_only()
        self.assertEqual(len(self.session_rows()), 2)  # the Swift writer's double
        self.index()
        self.assertEqual([r[2] for r in self.session_rows()], [str(b)])
        self.assertEqual(writer.status(self.home)["sessions_by_agent"], {"claude": 1})

    def test_the_double_is_gone_after_the_second_path_is_pruned_too(self):
        self.copy_row_at_a()
        b = self.resumed_at_b()
        self.index_live_only()
        self.mirror_only()
        b.unlink()
        self.index()
        self.assertEqual([r[2] for r in self.session_rows()], [str(b)])
        self.assertEqual(self.hits("plover"), [CLAUDE_ID])
        self.index()
        self.assertEqual(len(self.session_rows()), 1)

    def test_a_row_read_from_a_live_file_is_never_retired_by_a_live_sibling(self):
        # Never worse than no mirror: with no mirror at all the old row
        # under the pruned path stays beside the live one, so it stays.
        a = self.claude(CLAUDE_ID, "first question", folder="-work-a")
        self.index()
        a.unlink()
        self.resumed_at_b()
        self.index()
        self.assertEqual(len(self.session_rows()), 2)


class ForgetRaceTests(MirrorSearchCase):
    def copy_row(self):
        source = self.claude(CLAUDE_ID, "first question")
        self.mirror_only()
        copy = self.mirror_root / "claude/-work-proj" / source.name
        source.unlink()
        self.index()
        return source, copy

    def forget_while(self, act):
        """``forget_session`` with ``act`` run right after it first finds
        the row's path gone, before its delete, as the Mac writer (which
        takes no index.lock) or a restore can."""
        real = os.path.lexists
        done = []

        def interleaved(path):
            out = real(path)
            if not out and not done and CLAUDE_ID in str(path):
                done.append(1)
                act()
            return out
        with mock.patch.object(writer.os.path, "lexists", interleaved):
            return writer.forget_session(self.home, "claude", CLAUDE_ID)

    def test_a_transcript_restored_and_reindexed_meanwhile_keeps_its_row(self):
        source, copy = self.copy_row()

        def restore_grown():
            shutil.copy2(copy, source)
            self.grow(source, CLAUDE_ID, "plover restored", mtime=MTIME + 100)
            self.index_live_only()
        self.assertEqual(self.forget_while(restore_grown), 0)
        self.assertEqual(self.hits("plover"), [CLAUDE_ID])

    def test_a_transcript_restored_meanwhile_keeps_its_row_even_unchanged(self):
        source, copy = self.copy_row()
        self.assertEqual(self.forget_while(lambda: shutil.copy2(copy, source)), 0)
        self.assertEqual(self.hits("first"), [CLAUDE_ID])


class ForgetByPathTests(MirrorSearchCase):
    def test_a_transcript_named_without_a_session_id_is_forgotten_by_its_path(self):
        notes = write_jsonl(self.home / ".codex/sessions/2026/09/01/notes.jsonl",
                            codex_lines("", "redshank notes"))
        self.mirror_only()
        notes.unlink()
        self.index()
        self.assertEqual(len(self.hits("redshank")), 1)
        self.assertEqual(writer.forget_session(self.home, "codex", "notes"), 0)
        self.assertEqual(writer.forget_session(self.home, "codex", "notes",
                                               source_path=str(notes)), 1)
        self.assertEqual(self.hits("redshank"), [])


class CostTests(MirrorSearchCase):
    def prune(self, n):
        paths = [self.claude(f"{i:08d}-2222-3333-4444-555555555555", f"godwit {i}")
                 for i in range(n)]
        self.mirror_only()
        for path in paths:
            path.unlink()

    def test_an_unchanged_copy_is_not_read_again(self):
        self.prune(3)
        self.index()
        self.assertEqual(len(self.hits("godwit")), 3)
        with mock.patch.object(writer.Indexer, "_read", side_effect=AssertionError("read")):
            result = self.index()
        self.assertEqual(result.failed, 0)

    def test_directories_listed_do_not_grow_with_sessions(self):
        # Ten more pruned sessions in the same folder list no more
        # directories: the copies are found by one walk, not per session.
        def listings(n):
            self.home = temp_dir(self).resolve()
            self.mirror_root = transcripts_dir_for(self.home)
            self.prune(n)
            self.index()
            calls = []
            real = os.scandir

            def counting(path="."):
                calls.append(str(path))
                return real(path)
            with mock.patch("os.scandir", counting):
                self.index()
            return len(calls)

        self.assertEqual(listings(2), listings(12))


if __name__ == "__main__":
    unittest.main()
