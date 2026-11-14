"""Claude Code subagent transcripts in swe's transcript mirror (#1918).

Claude Code keeps a session's subagent transcripts beside it, in
``<project>/<session id>/subagents/agent-<id>.jsonl``, and prunes them
with the session. Their lines carry the parent's ``sessionId``, so both
ledgers count their usage under the parent session while the files are
live. The mirror copies them beside the parent's copy, so that usage
outlives the prune; nothing lists or searches them as sessions of their
own, as nothing does while they are live.

Everything runs in a throwaway HOME, with clones off so the streamed copy
path is what runs everywhere.
"""

import json
import os
import shutil
from pathlib import Path

from swe.ledger.scan import scan
from swe.ledger.store import LedgerStore, ledger_path_for
from swe.paths import transcripts_dir_for
from swe.sessions import search_writer as writer
from swe.sessions import transcript_mirror as mirror
from tests._tempdir import temp_dir
from tests.test_ledger_mirror import LedgerMirrorCase, claude_event
from tests.test_ledger_mirror import totals as ledger_totals
from tests.test_ledger_mirror import write_jsonl as write_usage
from tests.test_transcript_mirror import (
    CLAUDE_ID,
    MirrorCase,
    claude_records,
    write_jsonl,
)

AGENT = "agent-a1b2c3d4e5f607182"
OTHER_AGENT = "agent-a9f8e7d6c5b4a3921"


def subagent_records(prompt="look around"):
    """A subagent transcript as Claude Code writes it: the parent's
    session id on every line, ``isSidechain`` set."""
    records = claude_records(prompt)
    for record in records:
        record["isSidechain"] = True
        record["agentId"] = AGENT[len("agent-"):]
    return records


class SubagentMirrorCase(MirrorCase):
    def session_with_subagents(self) -> tuple[Path, list[Path]]:
        source = self.claude()
        folder = source.parent / CLAUDE_ID / "subagents"
        subs = [write_jsonl(folder / f"{AGENT}.jsonl", subagent_records("first")),
                write_jsonl(folder / f"{OTHER_AGENT}.jsonl", subagent_records("second"))]
        # Claude Code writes these beside each transcript; the mirror
        # copies transcripts only.
        (folder / f"{AGENT}.meta.json").write_text("{}")
        return source, subs

    def prune(self, source: Path) -> None:
        """What Claude Code's cleanup does: the transcript and its
        session folder go."""
        shutil.rmtree(source.parent / source.stem)
        source.unlink()

    def copy_of(self, live: Path) -> Path:
        rel = os.path.relpath(live, self.home / ".claude/projects")
        return self.mirror_root / "claude" / rel


class CopyTests(SubagentMirrorCase):
    def test_subagent_transcripts_are_copied_beside_the_session_and_recorded(self):
        source, subs = self.session_with_subagents()
        result = self.sync()
        self.assertEqual(result.failed, 0)
        self.assertEqual(result.copied, 3)
        manifest = mirror.load_manifest(self.home)
        for sub in subs:
            copy = self.copy_of(sub)
            self.assertEqual(copy.read_bytes(), sub.read_bytes())
            self.assertEqual(copy.stat().st_mtime_ns, sub.stat().st_mtime_ns)
            rel = f"claude/-work-proj/{CLAUDE_ID}/subagents/{sub.name}"
            self.assertEqual(manifest[rel]["source"], os.path.realpath(sub))
            self.assertEqual(manifest[rel]["harness"], "claude")
            self.assertEqual(manifest[rel]["account_home"], "")
        # Transcripts only: the sidecar is not copied.
        self.assertFalse((self.copy_of(subs[0]).parent / f"{AGENT}.meta.json").exists())

    def test_an_unchanged_subagent_is_not_copied_again_and_a_grown_one_is(self):
        _source, subs = self.session_with_subagents()
        self.sync()
        again = self.sync()
        self.assertEqual((again.copied, again.unchanged), (0, 3))
        write_jsonl(subs[0], subagent_records("first") + subagent_records("more"))
        grown = self.sync()
        self.assertEqual((grown.copied, grown.unchanged), (1, 2))
        self.assertEqual(self.copy_of(subs[0]).read_bytes(), subs[0].read_bytes())

    def test_a_pruned_subagent_copy_is_kept_and_counted_as_orphaned(self):
        source, subs = self.session_with_subagents()
        self.sync()
        self.prune(source)
        result = self.sync()
        self.assertEqual(result.orphaned, 3)
        for sub in subs:
            self.assertTrue(self.copy_of(sub).is_file())

    def test_an_account_home_session_keeps_its_subagents_in_its_account_folder(self):
        account = self.home / ".swe/accounts/claude/work/home"
        source = write_jsonl(account / "projects/-work-proj" / f"{CLAUDE_ID}.jsonl",
                             claude_records())
        sub = write_jsonl(source.parent / CLAUDE_ID / "subagents" / f"{AGENT}.jsonl",
                          subagent_records())
        self.sync()
        rel = f"accounts/claude/work/-work-proj/{CLAUDE_ID}/subagents/{AGENT}.jsonl"
        self.assertEqual((self.mirror_root / rel).read_bytes(), sub.read_bytes())
        self.assertEqual(mirror.load_manifest(self.home)[rel]["account_home"], str(account))


class LinkTests(SubagentMirrorCase):
    """A linked folder or file is never followed into a copy, as the
    mirror's own walk never follows one."""

    def test_a_linked_subagents_folder_is_not_copied(self):
        source = self.claude()
        elsewhere = temp_dir(self) / "subagents"
        write_jsonl(elsewhere / f"{AGENT}.jsonl", subagent_records())
        (source.parent / CLAUDE_ID).mkdir()
        (source.parent / CLAUDE_ID / "subagents").symlink_to(elsewhere, target_is_directory=True)
        self.sync()
        self.assertEqual(list(self.mirror_root.rglob(f"{AGENT}.jsonl")), [])

    def test_a_linked_session_folder_is_not_copied(self):
        source = self.claude()
        elsewhere = temp_dir(self) / "session"
        write_jsonl(elsewhere / "subagents" / f"{AGENT}.jsonl", subagent_records())
        (source.parent / CLAUDE_ID).symlink_to(elsewhere, target_is_directory=True)
        self.sync()
        self.assertEqual(list(self.mirror_root.rglob(f"{AGENT}.jsonl")), [])

    def test_a_linked_subagent_file_is_not_copied(self):
        source = self.claude()
        elsewhere = write_jsonl(temp_dir(self) / f"{AGENT}.jsonl", subagent_records())
        folder = source.parent / CLAUDE_ID / "subagents"
        folder.mkdir(parents=True)
        (folder / f"{AGENT}.jsonl").symlink_to(elsewhere)
        result = self.sync()
        self.assertEqual(result.failed, 0)
        self.assertEqual(list(self.mirror_root.rglob(f"{AGENT}.jsonl")), [])
        self.assertEqual(list(self.mirror_root.rglob("-external-*")), [])


class ListingTests(SubagentMirrorCase):
    def test_a_live_session_with_subagents_lists_once(self):
        self.session_with_subagents()
        self.sync()
        rows = self.sessions()
        self.assertEqual([r["id"] for r in rows], [CLAUDE_ID])

    def test_a_pruned_session_with_subagents_lists_once_and_mirrored(self):
        source, _subs = self.session_with_subagents()
        self.sync()
        self.prune(source)
        rows = self.sessions()
        self.assertEqual([(r["id"], r["mirrored"]) for r in rows], [(CLAUDE_ID, True)])


class SearchTests(SubagentMirrorCase):
    """Live subagent transcripts are not indexed, so their copies are not
    either: the pruned session keeps one row, filed under its source."""

    def rows(self) -> list[str]:
        conn = writer.open_index(writer.search_index_for(self.home))
        try:
            return sorted(r[0] for r in conn.execute("SELECT path FROM files"))
        finally:
            conn.close()

    def test_no_subagent_transcript_or_copy_is_indexed(self):
        source, _subs = self.session_with_subagents()
        writer.run_pass(self.home)
        live = self.rows()
        self.assertEqual(live, [os.path.realpath(source)])
        self.prune(source)
        writer.run_pass(self.home)
        self.assertEqual(self.rows(), live)


class RestoreTests(SubagentMirrorCase):
    def restore(self):
        return self.run_restore(CLAUDE_ID, "--json")

    def run_restore(self, *args):
        from tests.test_json_output import run_swe

        return run_swe(self.home, "session", "restore", *args)

    def test_restore_brings_the_subagent_transcripts_back(self):
        source, subs = self.session_with_subagents()
        before = {sub: sub.read_bytes() for sub in subs}
        self.sync()
        self.prune(source)
        result = self.restore()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        body = json.loads(result.stdout)["data"]["restored"]
        self.assertEqual(sorted(body["subagents"]), sorted(os.path.realpath(s) for s in subs))
        self.assertEqual(body["subagents_skipped"], [])
        for sub, data in before.items():
            self.assertEqual(sub.read_bytes(), data)
            self.assertEqual(sub.stat().st_mtime_ns, self.copy_of(sub).stat().st_mtime_ns)
        # Every restored file is the source again: the next pass finds
        # each copy current.
        again = self.sync()
        self.assertEqual((again.copied, again.unchanged, again.orphaned), (0, 3, 0))

    def test_an_existing_subagent_file_is_never_overwritten(self):
        source, subs = self.session_with_subagents()
        self.sync()
        self.prune(source)
        write_jsonl(subs[0], subagent_records("somebody else's"))
        kept = subs[0].read_bytes()
        result = self.restore()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        body = json.loads(result.stdout)["data"]["restored"]
        self.assertEqual(body["subagents"], [os.path.realpath(subs[1])])
        self.assertEqual([s["target"] for s in body["subagents_skipped"]],
                         [os.path.realpath(subs[0])])
        self.assertEqual(subs[0].read_bytes(), kept)
        self.assertTrue(subs[1].is_file())

    def test_a_subagent_entry_pointing_elsewhere_is_not_restored(self):
        source, subs = self.session_with_subagents()
        self.sync()
        self.prune(source)
        path = mirror.manifest_path(self.home)
        body = json.loads(path.read_text())
        rel = f"claude/-work-proj/{CLAUDE_ID}/subagents/{AGENT}.jsonl"
        stray = temp_dir(self) / "elsewhere" / f"{AGENT}.jsonl"
        body["files"][rel]["source"] = str(stray)
        path.write_text(json.dumps(body))
        result = self.restore()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertFalse(stray.exists())
        self.assertFalse(subs[0].exists())
        self.assertTrue(subs[1].is_file())


class PurgeTests(SubagentMirrorCase):
    def test_purge_removes_the_subagent_copies_and_their_entries(self):
        from tests.test_json_output import run_swe

        source, subs = self.session_with_subagents()
        self.sync()
        self.prune(source)
        plan = mirror.purge_plan(self.home, self._session())
        self.assertEqual(len(plan["entries"]), 3)
        result = run_swe(self.home, "session", "mirror", f"--purge={CLAUDE_ID}", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(list(self.mirror_root.rglob("*.jsonl")), [])
        self.assertEqual(mirror.load_manifest(self.home), {})

    def _session(self):
        from swe.sessions.models import Session

        return Session(timestamp=0, agent="", path="", session_id=CLAUDE_ID, tool_name="claude")


# -- the usage ledger ----------------------------------------------------


class LedgerSubagentTests(LedgerMirrorCase):
    """A subagent's usage is counted under its parent session, live or
    from its copy, exactly once."""

    def session(self, base: Path, parent=((1, 100, 10),), subs=(((101, 50, 5),),),
                pad=0) -> list[Path]:
        """The parent transcript and one subagent transcript per entry of
        ``subs``, every line carrying the parent's session id."""
        main = base / ".claude/projects/-work-proj" / f"{CLAUDE_ID}.jsonl"
        files = [write_usage(main, [claude_event(CLAUDE_ID, *e, pad=pad) for e in parent])]
        for index, events in enumerate(subs):
            path = main.parent / CLAUDE_ID / "subagents" / f"agent-a{index:016x}.jsonl"
            lines = []
            for e in events:
                line = claude_event(CLAUDE_ID, *e, pad=pad)
                line["isSidechain"] = True
                lines.append(line)
            files.append(write_usage(path, lines))
        return files

    def prune(self) -> None:
        folder = self.home / ".claude/projects/-work-proj"
        shutil.rmtree(folder / CLAUDE_ID)
        (folder / f"{CLAUDE_ID}.jsonl").unlink()

    def sessions(self, store=None) -> list[tuple]:
        store = store or self.store
        return store.db.execute(
            "SELECT DISTINCT harness, account, session FROM events").fetchall()

    def reference_store(self, **kw):
        home = temp_dir(self)
        store = LedgerStore(ledger_path_for(home))
        self.addCleanup(store.close)
        self.session(home, **kw)
        scan(store, home)
        return store, home

    def test_live_subagent_usage_is_attributed_to_the_parent_session(self):
        store, home = self.reference_store(subs=(((101, 50, 5), (102, 50, 5)),))
        self.assertEqual(ledger_totals(store), (3, 200, 20))
        self.assertEqual(self.sessions(store), [("claude", str(home / ".claude"), CLAUDE_ID)])

    def test_a_session_with_subagents_pruned_before_any_scan_keeps_both_usages(self):
        kw = {"subs": (((101, 50, 5), (102, 50, 5)), ((201, 7, 3),))}
        expected = ledger_totals(self.reference_store(**kw)[0])
        self.assertEqual(expected, (4, 207, 23))
        self.session(self.home, **kw)
        self.mirror_pass()
        self.prune()
        first = self.scan()
        self.assertEqual(first.errors, [])
        self.assertEqual(first.files_read, 3)
        self.assertEqual(self.totals(), expected)
        # Under the parent session and its harness home, as when live.
        self.assertEqual(self.sessions(), [("claude", str(self.home / ".claude"), CLAUDE_ID)])
        # Read once: each copy's own cursor keeps it from being read again.
        second = self.scan()
        self.assertEqual(second.files_read, 0)
        self.assertEqual(self.totals(), expected)

    def test_subagents_scanned_then_pruned_are_not_counted_twice(self):
        # Past the fingerprint's reach, so the copies are read again and
        # every line upserts onto the row it already has.
        kw = {"parent": [(n, 100, 10) for n in range(1, 21)],
              "subs": ([(100 + n, 50, 5) for n in range(1, 21)],), "pad": 600}
        files = self.session(self.home, **kw)
        self.assertGreater(files[1].stat().st_size, 2 * 4096)
        self.scan()
        before = self.totals()
        self.assertEqual(before, (40, 3000, 300))
        self.mirror_pass()
        self.prune()
        pruned = self.scan()
        self.assertEqual(pruned.errors, [])
        self.assertEqual(pruned.files_read, 2)
        self.assertEqual(self.totals(), before)
        self.assertEqual(self.scan().files_read, 0)
        self.assertEqual(self.totals(), before)

    def test_small_subagents_scanned_then_pruned_are_not_even_read(self):
        self.session(self.home)
        self.scan()
        before = self.totals()
        self.mirror_pass()
        self.prune()
        self.assertEqual(self.scan().files_read, 0)
        self.assertEqual(self.totals(), before)

    def test_a_live_subagents_copy_is_never_read(self):
        # The copy holds an event of its own, which would count if read.
        files = self.session(self.home)
        self.mirror_pass()
        copy = Path(os.path.realpath(transcripts_dir_for(self.home))) / "claude" / \
            os.path.relpath(files[1], self.home / ".claude/projects")
        self.assertTrue(copy.is_file())
        extra = claude_event(CLAUDE_ID, 999, 1000, 100)
        with copy.open("a") as handle:
            handle.write(json.dumps(extra, separators=(",", ":")) + "\n")
        self.scan()
        self.assertEqual(self.totals(), (2, 150, 15))
        keys = [r[0] for r in self.store.db.execute("SELECT path FROM files")]
        self.assertFalse([k for k in keys if k.startswith(
            os.path.realpath(transcripts_dir_for(self.home)))])

    def test_a_linked_subagents_folder_inside_the_mirror_is_not_read(self):
        self.session(self.home)
        self.mirror_pass()
        self.prune()
        tree = Path(os.path.realpath(transcripts_dir_for(self.home))) / \
            f"claude/-work-proj/{CLAUDE_ID}/subagents"
        elsewhere = temp_dir(self) / "subagents"
        shutil.move(str(tree), str(elsewhere))
        tree.symlink_to(elsewhere, target_is_directory=True)
        result = self.scan()
        self.assertEqual(result.errors, [])
        self.assertEqual(self.totals(), (1, 100, 10))

    def test_a_lost_manifest_still_counts_the_subagent_once(self):
        self.session(self.home)
        self.mirror_pass()
        self.prune()
        mirror.manifest_path(self.home).unlink()
        self.scan()
        self.scan()
        self.assertEqual(self.totals(), (2, 150, 15))


if __name__ == "__main__":
    import unittest

    unittest.main()
