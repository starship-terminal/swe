"""Restore, purge, temp sweep, doctor and hub chat for the transcript
mirror (#1918 follow-ups).

Everything runs in a throwaway HOME. The properties under test: restore
never overwrites a file and only writes where the source was; purge
removes only the files the manifest ties to the one session the user
named, never through a symlink and never for a live session; a mirrored
session's chat reads from its copy and never tails it.
"""

import json
import os
import time
import unittest
from pathlib import Path
from unittest import mock

from swe.paths import transcripts_dir_for
from swe.sessions import transcript_mirror as mirror
from swe.sessions.models import Session
from tests._tempdir import temp_dir
from tests.test_json_output import decode, run_swe
from tests.test_transcript_mirror import (
    ARCHIVED_STEM,
    ARCHIVED_UUID,
    CLAUDE_ID,
    CODEX_STEM,
    CODEX_UUID,
    MTIME,
    MirrorCase,
    claude_records,
    codex_records,
    write_jsonl,
)

OTHER_ID = "99999999-8888-7777-6666-555555555555"


class RestoreCase(MirrorCase):
    def mirrored_claude(self, **kw) -> Path:
        source = self.claude(**kw)
        self.sync()
        source.unlink()
        return source

    def restore(self, *args):
        return run_swe(self.home, "session", "restore", *args)

    def row(self, session_id=CLAUDE_ID) -> dict:
        rows = [r for r in self.sessions() if r["id"] == session_id]
        self.assertEqual(len(rows), 1, rows)
        return rows[0]


class RestoreTests(RestoreCase):
    def test_restore_puts_the_copy_back_where_the_source_was(self):
        source = self.mirrored_claude()
        copy = self.mirror_root / "claude/-work-proj" / source.name
        result = self.restore(CLAUDE_ID)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(source.read_bytes(), copy.read_bytes())
        # The restored file keeps the session's timestamp.
        self.assertEqual(source.stat().st_mtime_ns, copy.stat().st_mtime_ns)
        self.assertIn(str(source.name), result.stdout)

    def test_a_restored_session_lists_live_and_resumable(self):
        self.mirrored_claude()
        self.assertTrue(self.row()["mirrored"])
        self.assertEqual(self.restore(CLAUDE_ID).returncode, 0)
        row = self.row()
        self.assertFalse(row["mirrored"])
        self.assertEqual(row["resume"]["argv"], ["claude", "--resume", CLAUDE_ID])

    def test_the_copy_stays_and_the_next_pass_finds_it_current(self):
        source = self.mirrored_claude()
        self.restore(CLAUDE_ID)
        self.assertTrue((self.mirror_root / "claude/-work-proj" / source.name).is_file())
        result = self.sync()
        self.assertEqual((result.copied, result.cloned, result.unchanged), (0, 0, 1))

    def test_an_append_right_after_the_restore_reads_as_changed(self):
        source = self.mirrored_claude()
        copy = self.mirror_root / "claude/-work-proj" / source.name
        written = copy.stat().st_size
        real_link = mirror._link_new

        def link_then_append(tmp, dest):
            real_link(tmp, dest)
            with open(dest, "a") as fh:  # the harness resumes at once
                fh.write(json.dumps(claude_records("new reply")[0]) + "\n")

        session = Session(timestamp=1.0, agent="Claude Code", path="/work/proj",
                          session_id=CLAUDE_ID, tool_name="claude", mirrored=True)
        with mock.patch.object(mirror, "_link_new", side_effect=link_then_append):
            mirror.restore(self.home, session)
        entry = mirror.load_manifest(self.home)[f"claude/-work-proj/{source.name}"]
        self.assertEqual(entry["size"], written)
        result = self.sync()
        self.assertEqual(result.copied + result.cloned, 1)
        self.assertEqual(copy.read_bytes(), source.read_bytes())

    def test_an_existing_file_is_never_overwritten(self):
        source = self.mirrored_claude()
        source.write_text("a new transcript the harness started\n")
        result = self.restore(CLAUDE_ID)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(source.read_text(), "a new transcript the harness started\n")
        # The listing now calls it live; the logic refuses on its own too,
        # for a caller holding a stale mirrored row.
        stale = Session(timestamp=1.0, agent="Claude Code", path="/work/proj",
                        session_id=CLAUDE_ID, tool_name="claude", mirrored=True)
        with self.assertRaisesRegex(mirror.Refused, "already exists"):
            mirror.restore(self.home, stale)
        self.assertEqual(source.read_text(), "a new transcript the harness started\n")

    def test_a_live_session_is_refused(self):
        source = self.claude()
        self.sync()
        before = source.read_bytes()
        result = self.restore(CLAUDE_ID)
        self.assertEqual(result.returncode, 1)
        self.assertIn("not mirrored", result.stdout + result.stderr)
        self.assertEqual(source.read_bytes(), before)

    def test_an_unknown_id_is_refused(self):
        result = self.restore("nope")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no session", (result.stdout + result.stderr).lower())

    def test_the_project_folder_is_created_again(self):
        source = self.mirrored_claude()
        source.parent.rmdir()
        self.assertEqual(self.restore(CLAUDE_ID).returncode, 0)
        self.assertTrue(source.is_file())

    def test_the_listing_index_names_the_session(self):
        source = self.mirrored_claude()
        result = self.restore("1")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(source.is_file())

    def test_json_reports_what_was_restored_and_where(self):
        source = self.mirrored_claude()
        result = self.restore(CLAUDE_ID, "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        restored = decode(result)["data"]["restored"]
        self.assertEqual(restored["target"], str(source))
        self.assertEqual(restored["copy"],
                         str(Path(os.path.realpath(self.mirror_root)) / "claude/-work-proj"
                             / source.name))
        self.assertEqual((restored["harness"], restored["session_id"]), ("claude", CLAUDE_ID))
        self.assertEqual(restored["bytes"], source.stat().st_size)

    def test_a_refusal_under_json_leaves_stdout_empty(self):
        self.claude()
        self.sync()
        result = self.restore(CLAUDE_ID, "--json")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")

    def test_an_ambiguous_restore_takes_the_account_flag(self):
        default = self.mirrored_claude()
        account = self.home / ".swe/accounts/claude/work/home"
        in_account = write_jsonl(account / "projects/-work-proj" / f"{CLAUDE_ID}.jsonl",
                                 claude_records("account text"))
        self.sync()
        in_account.unlink()
        refused = self.restore(CLAUDE_ID)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("--account=", refused.stdout + refused.stderr)
        result = self.restore(CLAUDE_ID, f"--account={account}")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(in_account.is_file())
        self.assertFalse(default.exists())

    def test_a_codex_thread_id_restores_its_dated_rollout(self):
        codex = self.codex()
        self.sync()
        codex.unlink()
        result = self.restore(CODEX_UUID)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(codex.is_file())
        rows = [r for r in self.sessions() if r["id"] == CODEX_STEM]
        self.assertFalse(rows[0]["mirrored"])
        self.assertEqual(rows[0]["resume"]["argv"], ["codex", "resume", CODEX_UUID])

    def test_an_archived_rollout_goes_back_to_archived_sessions(self):
        archived = self.archived()
        self.sync()
        archived.unlink()
        self.assertEqual(self.restore(ARCHIVED_STEM).returncode, 0)
        self.assertTrue(archived.is_file())
        self.assertEqual(archived.parent.name, "archived_sessions")

    def test_an_account_home_session_restores_into_that_home(self):
        account = self.home / ".swe/accounts/claude/work/home"
        source = write_jsonl(account / "projects/-work-proj" / f"{CLAUDE_ID}.jsonl",
                             claude_records())
        self.sync()
        source.unlink()
        self.assertEqual(self.restore(CLAUDE_ID).returncode, 0)
        self.assertTrue(source.is_file())
        self.assertFalse((self.home / ".claude/projects/-work-proj" / source.name).exists())

    def test_with_several_copies_the_listed_one_is_restored(self):
        older = write_jsonl(self.home / ".claude/projects/-a-older" / f"{CLAUDE_ID}.jsonl",
                            claude_records("older text"), MTIME)
        newer = write_jsonl(self.home / ".claude/projects/-z-newer" / f"{CLAUDE_ID}.jsonl",
                            claude_records("newer text"), MTIME + 50)
        self.sync()
        older.unlink()
        newer.unlink()
        self.assertEqual(self.row()["title"], "newer text")
        self.assertEqual(self.restore(CLAUDE_ID).returncode, 0)
        self.assertTrue(newer.is_file())
        self.assertFalse(older.exists())

    def test_a_source_that_is_not_a_path_is_refused(self):
        self.mirrored_claude()
        manifest = json.loads(mirror.manifest_path(self.home).read_text())
        for entry in manifest["files"].values():
            entry["source"] = 12
        mirror.manifest_path(self.home).write_text(json.dumps(manifest))
        session = Session(timestamp=1.0, agent="Claude Code", path="/work/proj",
                          session_id=CLAUDE_ID, tool_name="claude", mirrored=True)
        with self.assertRaises(mirror.Refused):
            mirror.restore(self.home, session)

    def test_a_copy_the_manifest_does_not_know_is_refused(self):
        source = self.mirrored_claude()
        mirror.manifest_path(self.home).unlink()
        result = self.restore(CLAUDE_ID)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(source.exists())
        self.assertIn("manifest", result.stdout + result.stderr)

    def test_an_external_source_whose_link_remains_is_restored(self):
        elsewhere = temp_dir(self).resolve()
        target = write_jsonl(elsewhere / f"{CLAUDE_ID}.jsonl", claude_records())
        link = self.home / ".claude/projects/-work-proj" / target.name
        link.parent.mkdir(parents=True)
        link.symlink_to(target)
        self.sync()
        target.unlink()  # the link dangles
        self.assertTrue(self.row()["mirrored"])
        result = self.restore(CLAUDE_ID)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(target.is_file())
        self.assertFalse(self.row()["mirrored"])

    def test_an_external_source_whose_link_is_gone_is_refused(self):
        elsewhere = temp_dir(self).resolve()
        target = write_jsonl(elsewhere / f"{CLAUDE_ID}.jsonl", claude_records())
        link = self.home / ".claude/projects/-work-proj" / target.name
        link.parent.mkdir(parents=True)
        link.symlink_to(target)
        self.sync()
        link.unlink()
        target.unlink()
        result = self.restore(CLAUDE_ID)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(target.exists())
        # Still listed, from the copy.
        self.assertTrue(self.row()["mirrored"])

    def test_an_external_source_whose_folder_is_gone_is_refused(self):
        elsewhere = temp_dir(self).resolve() / "gone"
        target = write_jsonl(elsewhere / f"{CLAUDE_ID}.jsonl", claude_records())
        link = self.home / ".claude/projects/-work-proj" / target.name
        link.parent.mkdir(parents=True)
        link.symlink_to(target)
        self.sync()
        target.unlink()
        elsewhere.rmdir()
        self.assertEqual(self.restore(CLAUDE_ID).returncode, 1)
        self.assertFalse(elsewhere.exists())

    def test_a_symlinked_copy_inside_the_mirror_is_refused(self):
        source = self.mirrored_claude()
        copy = self.mirror_root / "claude/-work-proj" / source.name
        outside = write_jsonl(temp_dir(self) / "secret.jsonl", claude_records("secret"))
        copy.unlink()
        copy.symlink_to(outside)
        self.assertEqual(self.restore(CLAUDE_ID).returncode, 1)
        self.assertFalse(source.exists())

    def test_a_copy_folder_linked_to_another_account_is_refused(self):
        source = self.mirrored_claude()
        account = self.home / ".swe/accounts/claude/work/home"
        in_account = write_jsonl(account / "projects/-work-proj" / f"{CLAUDE_ID}.jsonl",
                                 claude_records("account text"))
        self.sync()
        in_account.unlink()
        folder = self.mirror_root / "claude/-work-proj"
        (folder / source.name).unlink()
        folder.rmdir()
        folder.symlink_to(self.mirror_root / "accounts/claude/work/-work-proj")
        session = Session(timestamp=1.0, agent="Claude Code", path="/work/proj",
                          session_id=CLAUDE_ID, tool_name="claude", mirrored=True)
        with self.assertRaises(mirror.Refused):
            mirror.restore(self.home, session)
        self.assertFalse(in_account.exists())
        self.assertFalse(source.exists())

    def test_a_parent_swapped_for_a_link_is_refused(self):
        source = self.mirrored_claude()
        source.parent.rmdir()
        elsewhere = temp_dir(self)
        source.parent.symlink_to(elsewhere)
        self.assertEqual(self.restore(CLAUDE_ID).returncode, 1)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_no_folder_is_created_through_a_swapped_link(self):
        source = self.mirrored_claude()
        source.parent.rmdir()
        projects = source.parent.parent
        projects.rmdir()
        elsewhere = temp_dir(self)
        projects.symlink_to(elsewhere)
        self.assertEqual(self.restore(CLAUDE_ID).returncode, 1)
        self.assertEqual(list(elsewhere.iterdir()), [])


class ExclusiveCopyTests(RestoreCase):
    def test_the_final_step_never_replaces_a_file_that_appeared(self):
        source = write_jsonl(temp_dir(self) / "a.jsonl", claude_records())
        dest = self.home / ".claude/projects/-p/a.jsonl"
        dest.parent.mkdir(parents=True)
        # The harness writes the file between the checks and the rename.
        with mock.patch.object(mirror.os, "fsync",
                               side_effect=lambda fd: dest.write_text("harness\n")):
            with self.assertRaises(FileExistsError):
                mirror._copy(str(source), dest, self.home, exclusive=True)
        self.assertEqual(dest.read_text(), "harness\n")
        self.assertEqual([p.name for p in dest.parent.iterdir()], ["a.jsonl"])


class PurgeCase(RestoreCase):
    def purge(self, *args):
        return run_swe(self.home, "session", "mirror", *args)


class PurgeTests(PurgeCase):
    def test_purge_removes_the_copy_its_backup_and_its_manifest_entry(self):
        source = self.claude()
        self.sync()
        rel = f"claude/-work-proj/{source.name}"
        copy = self.mirror_root / rel
        backup = copy.with_name(copy.name + ".shrunk-123")
        os.link(copy, backup)
        source.unlink()
        result = self.purge(f"--purge={CLAUDE_ID}", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertFalse(copy.exists())
        self.assertFalse(backup.exists())
        self.assertNotIn(rel, mirror.load_manifest(self.home))
        # It prints what it removes.
        self.assertIn(copy.name, result.stdout)
        self.assertIn(backup.name, result.stdout)
        self.assertEqual([r for r in self.sessions() if r["id"] == CLAUDE_ID], [])

    def test_backups_of_a_copy_already_gone_still_go(self):
        source = self.claude()
        self.sync()
        copy = self.mirror_root / f"claude/-work-proj/{source.name}"
        backup = copy.with_name(copy.name + ".shrunk-123")
        os.link(copy, backup)
        source.unlink()
        # An interrupted purge removed the copy but not its backup.
        copy.unlink()
        session = Session(timestamp=1.0, agent="Claude Code", path="/work/proj",
                          session_id=CLAUDE_ID, tool_name="claude", mirrored=True)
        plan = mirror.purge_plan(self.home, session)
        self.assertEqual(plan["files"], [str(Path(os.path.realpath(backup)))])
        mirror.purge(self.home, plan)
        self.assertFalse(backup.exists())
        self.assertEqual(mirror.load_manifest(self.home), {})

    def test_without_yes_and_no_terminal_nothing_is_removed(self):
        source = self.mirrored_claude()
        copy = self.mirror_root / "claude/-work-proj" / source.name
        result = self.purge(f"--purge={CLAUDE_ID}")
        self.assertEqual(result.returncode, 1)
        self.assertTrue(copy.is_file())
        self.assertIn(copy.name, result.stdout)  # the plan is still shown

    def test_a_live_session_is_refused(self):
        source = self.claude()
        self.sync()
        copy = self.mirror_root / "claude/-work-proj" / source.name
        result = self.purge(f"--purge={CLAUDE_ID}", "--yes")
        self.assertEqual(result.returncode, 1)
        self.assertTrue(copy.is_file())
        self.assertTrue(source.is_file())

    def test_other_sessions_and_accounts_are_left_alone(self):
        self.claude()
        other = self.claude(session_id=OTHER_ID)
        account = self.home / ".swe/accounts/claude/work/home"
        third = "77777777-6666-5555-4444-333333333333"
        in_account = write_jsonl(account / "projects/-work-proj" / f"{third}.jsonl",
                                 claude_records())
        self.sync()
        (self.home / ".claude/projects/-work-proj" / f"{CLAUDE_ID}.jsonl").unlink()
        other.unlink()
        in_account.unlink()
        before = set(mirror.load_manifest(self.home))
        result = self.purge(f"--purge={CLAUDE_ID}", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        after = set(mirror.load_manifest(self.home))
        self.assertEqual(before - after, {f"claude/-work-proj/{CLAUDE_ID}.jsonl"})
        self.assertTrue((self.mirror_root / f"claude/-work-proj/{OTHER_ID}.jsonl").is_file())
        self.assertTrue((self.mirror_root
                         / f"accounts/claude/work/-work-proj/{third}.jsonl").is_file())

    def test_a_row_number_is_refused(self):
        source = self.mirrored_claude()
        result = self.purge("--purge=1", "--yes")
        self.assertEqual(result.returncode, 1)
        self.assertIn("session id", result.stdout + result.stderr)
        self.assertTrue((self.mirror_root / "claude/-work-proj" / source.name).is_file())

    def both_homes(self):
        """One Claude id mirrored in the default home and in account "work"."""
        default = self.mirrored_claude()
        account = self.home / ".swe/accounts/claude/work/home"
        in_account = write_jsonl(account / "projects/-work-proj" / f"{CLAUDE_ID}.jsonl",
                                 claude_records("account text"))
        self.sync()
        in_account.unlink()
        return default, in_account

    def test_an_ambiguous_id_names_the_account_flag_and_both_homes(self):
        self.both_homes()
        result = self.purge(f"--purge={CLAUDE_ID}", "--yes")
        self.assertEqual(result.returncode, 1)
        said = result.stdout + result.stderr
        self.assertIn("--account=", said)
        self.assertIn("default", said)
        self.assertIn("work", said)

    def test_the_account_flag_picks_the_account_row(self):
        self.both_homes()
        result = self.purge(f"--purge={CLAUDE_ID}", "--account=work", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(set(mirror.load_manifest(self.home)),
                         {f"claude/-work-proj/{CLAUDE_ID}.jsonl"})

    def test_the_account_flag_picks_the_default_row(self):
        self.both_homes()
        result = self.purge(f"--purge={CLAUDE_ID}", "--account=default", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(set(mirror.load_manifest(self.home)),
                         {f"accounts/claude/work/-work-proj/{CLAUDE_ID}.jsonl"})

    def test_an_unknown_account_is_refused(self):
        self.both_homes()
        result = self.purge(f"--purge={CLAUDE_ID}", "--account=nope", "--yes")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(mirror.load_manifest(self.home)), 2)

    def test_the_account_flag_only_goes_with_purge(self):
        self.mirrored_claude()
        self.assertEqual(self.purge("--account=work").returncode, 1)

    def test_a_symlinked_copy_is_never_followed_out_of_the_mirror(self):
        source = self.mirrored_claude()
        copy = self.mirror_root / "claude/-work-proj" / source.name
        outside = write_jsonl(temp_dir(self) / f"{CLAUDE_ID}.jsonl", claude_records())
        copy.unlink()
        copy.symlink_to(outside)
        self.purge(f"--purge={CLAUDE_ID}", "--yes")
        self.assertTrue(outside.is_file())

    def test_a_symlinked_folder_is_never_followed_out_of_the_mirror(self):
        source = self.mirrored_claude()
        folder = self.mirror_root / "claude/-work-proj"
        outside = temp_dir(self)
        for path in folder.iterdir():
            os.replace(path, outside / path.name)
        folder.rmdir()
        folder.symlink_to(outside)
        self.purge(f"--purge={CLAUDE_ID}", "--yes")
        self.assertTrue((outside / source.name).is_file())

    def test_a_manifest_key_spelled_with_dot_dot_is_never_followed(self):
        source = self.mirrored_claude()
        victim = write_jsonl(self.home / "victim" / source.name, claude_records())
        manifest = json.loads(mirror.manifest_path(self.home).read_text())
        entry = manifest["files"][f"claude/-work-proj/{source.name}"]
        manifest["files"][f"claude/-work-proj/../../../../../victim/{source.name}"] = dict(entry)
        mirror.manifest_path(self.home).write_text(json.dumps(manifest))
        self.purge(f"--purge={CLAUDE_ID}", "--yes")
        self.assertTrue(victim.is_file())

    def test_every_rollout_of_a_codex_thread_goes(self):
        first = self.codex()
        resumed = self.codex(stem=f"rollout-2026-09-03T10-00-00-{CODEX_UUID}")
        self.sync()
        first.unlink()
        resumed.unlink()
        result = self.purge(f"--purge={CODEX_UUID}", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(list((self.mirror_root / "codex").rglob("*.jsonl")), [])
        self.assertEqual(mirror.load_manifest(self.home), {})

    def test_an_untracked_rollout_of_the_thread_is_named_and_left(self):
        first = self.codex()
        self.sync()
        first.unlink()
        stray = write_jsonl(self.mirror_root / "codex/2026/09/05"
                            / f"rollout-2026-09-05T10-00-00-{CODEX_UUID}.jsonl",
                            codex_records(), MTIME - 10)
        result = self.purge(f"--purge={CODEX_UUID}", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(stray.is_file())
        self.assertIn(stray.name, result.stdout)
        self.assertIn("keep listing", result.stdout)

    def test_subagent_copies_of_the_session_go_too(self):
        source = self.claude()
        sub = write_jsonl(source.parent / CLAUDE_ID / "subagents/agent-a1.jsonl",
                          claude_records("sub"))
        self.sync()
        sub.unlink()
        source.unlink()
        result = self.purge(f"--purge={CLAUDE_ID}", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(list(self.mirror_root.rglob("*.jsonl")), [])

    def test_json_needs_yes_and_reports_what_went(self):
        source = self.mirrored_claude()
        refused = self.purge(f"--purge={CLAUDE_ID}", "--json")
        self.assertEqual(refused.returncode, 1)
        self.assertEqual(refused.stdout, "")
        result = self.purge(f"--purge={CLAUDE_ID}", "--yes", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        purged = decode(result)["data"]["purged"]
        real_root = Path(os.path.realpath(self.mirror_root))
        self.assertEqual(purged["removed"], [str(real_root / "claude/-work-proj" / source.name)])
        self.assertEqual(purged["entries"], [f"claude/-work-proj/{source.name}"])

    def test_purge_cannot_be_combined_with_other_actions(self):
        self.mirrored_claude()
        self.assertEqual(self.purge(f"--purge={CLAUDE_ID}", "--off", "--yes").returncode, 1)
        self.assertEqual(self.purge(f"--purge={CLAUDE_ID}", "--sweep-tmp").returncode, 1)
        self.assertTrue(mirror.load_manifest(self.home))

    def test_a_copy_the_manifest_does_not_know_is_named_and_left(self):
        source = self.mirrored_claude()
        stray = write_jsonl(self.mirror_root / "claude/-other" / source.name,
                            claude_records(), MTIME - 5)
        result = self.purge(f"--purge={CLAUDE_ID}", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(stray.is_file())
        self.assertIn("-other", result.stdout)

    def test_a_plan_that_changed_before_the_lock_removes_nothing(self):
        source = self.mirrored_claude()
        copy = self.mirror_root / "claude/-work-proj" / source.name
        session = Session(timestamp=1.0, agent="Claude Code", path="/work/proj",
                          session_id=CLAUDE_ID, tool_name="claude", mirrored=True)
        plan = mirror.purge_plan(self.home, session)
        source.write_text("the harness wrote it again\n")
        with self.assertRaises(mirror.Refused):
            mirror.purge(self.home, plan)
        self.assertTrue(copy.is_file())

    def purged_session(self):
        return Session(timestamp=1.0, agent="Claude Code", path="/work/proj",
                       session_id=CLAUDE_ID, tool_name="claude", mirrored=True)

    def test_search_forgets_the_session_after_its_copies_are_gone(self):
        # A three-argument forget_session (an older search writer) still works.
        from swe.sessions import search_writer

        source = self.mirrored_claude()
        copy = self.mirror_root / "claude/-work-proj" / source.name
        calls = []

        def forget(home, harness, session_id):
            calls.append((harness, session_id, copy.exists()))

        with mock.patch.object(search_writer, "forget_session", forget, create=True):
            done = mirror.purge(self.home, mirror.purge_plan(self.home, self.purged_session()))
        self.assertEqual(calls, [("claude", CLAUDE_ID, False)])
        self.assertEqual(done["search"], "forgotten")

    def test_search_forgets_a_codex_thread_by_its_thread_id(self):
        from swe.sessions import search_writer

        first = self.codex()
        resumed = self.codex(stem=f"rollout-2026-09-03T10-00-00-{CODEX_UUID}")
        self.sync()
        first.unlink()
        resumed.unlink()
        calls = []
        session = Session(timestamp=1.0, agent="Codex CLI", path="/work/other",
                          session_id=resumed.stem, tool_name="codex", mirrored=True)
        with mock.patch.object(search_writer, "forget_session", create=True,
                               side_effect=lambda h, harness, sid: calls.append((harness, sid))):
            mirror.purge(self.home, mirror.purge_plan(self.home, session))
        self.assertEqual(calls, [("codex", CODEX_UUID)])

    def test_search_is_told_each_purged_entry_and_its_source(self):
        from swe.sessions import search_writer

        calls = []

        def forget(home, harness, session_id, source_path=None):
            calls.append((harness, session_id, source_path))
            return 0

        claude = self.mirrored_claude()
        first = self.codex()
        resumed = self.codex(stem=f"rollout-2026-09-03T10-00-00-{CODEX_UUID}")
        notes = write_jsonl(self.home / ".codex/archived_sessions/notes.jsonl",
                            codex_records(prompt="loose notes"), MTIME + 7)
        self.sync()
        for path in (first, resumed, notes):
            path.unlink()
        sessions = {
            "claude": Session(timestamp=1.0, agent="Claude Code", path="/work/proj",
                              session_id=CLAUDE_ID, tool_name="claude", mirrored=True),
            "thread": Session(timestamp=1.0, agent="Codex CLI", path="/work/other",
                              session_id=resumed.stem, tool_name="codex", mirrored=True),
            "notes": Session(timestamp=1.0, agent="Codex CLI", path="/work/other",
                             session_id="notes", tool_name="codex", mirrored=True),
        }
        with mock.patch.object(search_writer, "forget_session", forget, create=True):
            for session in sessions.values():
                mirror.purge(self.home, mirror.purge_plan(self.home, session))
        self.assertEqual(sorted(calls), sorted([
            ("claude", CLAUDE_ID, str(claude)),
            ("codex", CODEX_UUID, str(first)),
            ("codex", CODEX_UUID, str(resumed)),
            ("codex", "notes", str(notes)),
        ]))

    def test_a_busy_index_does_not_undo_the_purge(self):
        from swe.sessions import search_writer

        source = self.mirrored_claude()
        with mock.patch.object(search_writer, "forget_session", create=True,
                               side_effect=RuntimeError("index busy")):
            done = mirror.purge(self.home, mirror.purge_plan(self.home, self.purged_session()))
        self.assertIn("index busy", done["search"])
        self.assertFalse((self.mirror_root / "claude/-work-proj" / source.name).exists())


class StatusTests(PurgeCase):
    def test_a_manifest_with_bad_numbers_still_reports_status(self):
        self.mirrored_claude()
        manifest = json.loads(mirror.manifest_path(self.home).read_text())
        for entry in manifest["files"].values():
            entry.update(mirror_size="big")
        manifest["files"]["claude/-bad/x.jsonl"] = {"harness": "claude", "source": 12}
        mirror.manifest_path(self.home).write_text(json.dumps(manifest))
        result = self.purge("--status", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = decode(result)["data"]
        # The unreadable entry is not counted; the bad size reads as 0.
        self.assertEqual((data["entries"], data["bytes"]), (1, 0))
        self.assertEqual(self.purge("--status").returncode, 0)


class MalformedManifestTests(PurgeCase):
    """One bad manifest entry must never break a reader, and is never
    dropped from the file or allowed to cost its copy."""

    BAD = {
        "claude/-bad/not-a-dict.jsonl": 5,
        "claude/-bad/no-path.jsonl": {"harness": "claude", "source": 12, "mirror_size": 9},
        "claude/-bad/no-number.jsonl": {"harness": "claude", "source": "/gone/x.jsonl",
                                        "mirror_size": "big", "size": "x", "ino": None},
    }

    def poison(self):
        manifest = json.loads(mirror.manifest_path(self.home).read_text())
        manifest["files"].update(json.loads(json.dumps(self.BAD)))
        mirror.manifest_path(self.home).write_text(json.dumps(manifest))

    def assert_bad_entries_kept(self):
        files = json.loads(mirror.manifest_path(self.home).read_text())["files"]
        for key, value in self.BAD.items():
            self.assertEqual(files.get(key), value, key)

    def test_the_listing_survives(self):
        self.mirrored_claude()
        self.poison()
        listed = run_swe(self.home, "session")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual([r["id"] for r in self.sessions()], [CLAUDE_ID])
        self.assert_bad_entries_kept()

    def test_a_mirror_pass_keeps_bad_entries_verbatim(self):
        self.mirrored_claude()
        self.claude(session_id=OTHER_ID)
        self.poison()
        result = self.sync()
        self.assertEqual(result.copied + result.cloned, 1)
        self.assert_bad_entries_kept()
        self.assertEqual(self.purge("--json").returncode, 0)
        self.assert_bad_entries_kept()

    def test_a_copy_whose_entry_is_unreadable_is_never_overwritten(self):
        source = self.claude()
        self.sync()
        rel = f"claude/-work-proj/{source.name}"
        manifest = json.loads(mirror.manifest_path(self.home).read_text())
        manifest["files"][rel]["source"] = 12
        mirror.manifest_path(self.home).write_text(json.dumps(manifest))
        copy = self.mirror_root / rel
        copy.write_text("only copy of older history\n")
        write_jsonl(source, claude_records("changed since"), MTIME + 99)
        result = self.sync()
        self.assertEqual(copy.read_text(), "only copy of older history\n")
        self.assertEqual(result.unreadable, 1)
        files = json.loads(mirror.manifest_path(self.home).read_text())["files"]
        self.assertEqual(files[rel]["source"], 12)

    def test_restore_survives_and_keeps_bad_entries(self):
        source = self.mirrored_claude()
        self.poison()
        result = self.restore(CLAUDE_ID)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(source.is_file())
        self.assert_bad_entries_kept()

    def test_purge_survives_and_keeps_bad_entries(self):
        self.mirrored_claude()
        self.poison()
        result = self.purge(f"--purge={CLAUDE_ID}", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertNotIn(f"claude/-work-proj/{CLAUDE_ID}.jsonl", mirror.load_manifest(self.home))
        self.assert_bad_entries_kept()

    def test_doctor_counts_entries_it_could_not_read(self):
        self.mirrored_claude()
        self.poison()
        self.assertEqual(mirror.audit(self.home)["unreadable"], 2)
        data = decode(run_swe(self.home, "doctor", "--json"))["data"]
        self.assertIn("mirror.unreadable", {f["check"] for f in data["findings"]})
        self.assert_bad_entries_kept()


class SweepTests(PurgeCase):
    def temp(self, folder: Path, name: str, age: float) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_text("partial")
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
        return path

    def test_only_old_temp_files_of_the_mirrors_own_naming_go(self):
        self.mirrored_claude()
        folder = self.mirror_root / "claude/-work-proj"
        hexid = "0" * 32
        old = self.temp(folder, f".{CLAUDE_ID}.jsonl.123-{hexid}.tmp", 3 * 86400)
        fresh = self.temp(folder, f".{CLAUDE_ID}.jsonl.124-{'1' * 32}.tmp", 60)
        other = self.temp(folder, "notes.tmp", 3 * 86400)
        state = self.temp(self.mirror_root, ".manifest.json.abc123xy.tmp", 3 * 86400)
        target = self.temp(temp_dir(self), "target.tmp", 3 * 86400)
        link = folder / f".{OTHER_ID}.jsonl.125-{'2' * 32}.tmp"
        link.symlink_to(target)
        # ctime is "now" for files a test just wrote; the sweep must take
        # age from the newer of mtime and ctime, so pin ctime too.
        with mock.patch.object(mirror, "_age", side_effect=lambda st, now: now - st.st_mtime):
            removed = mirror.sweep_temps(self.home)
        self.assertEqual(sorted(removed), sorted([str(Path(os.path.realpath(old))),
                                                  str(Path(os.path.realpath(state)))]))
        self.assertFalse(old.exists())
        self.assertFalse(state.exists())
        for kept in (fresh, other, target):
            self.assertTrue(kept.exists())
        self.assertTrue(link.is_symlink())
        self.assertTrue(mirror.load_manifest(self.home))

    def test_a_restores_own_leftovers_in_recorded_source_folders_go(self):
        source = self.mirrored_claude()
        hexid = "c" * 32
        leftover = self.temp(source.parent, f".{source.name}.77-{hexid}.tmp", 3 * 86400)
        fresh = self.temp(source.parent, f".{source.name}.78-{'d' * 32}.tmp", 60)
        other_name = self.temp(source.parent, f".{OTHER_ID}.jsonl.79-{hexid}.tmp", 3 * 86400)
        elsewhere = self.temp(self.home / ".claude/projects/-other",
                              f".{source.name}.80-{hexid}.tmp", 3 * 86400)
        user_file = self.temp(source.parent, "notes.txt", 3 * 86400)
        with mock.patch.object(mirror, "_age", side_effect=lambda st, now: now - st.st_mtime):
            self.assertIn(str(leftover), mirror.audit(self.home)["stale_temps"])
            removed = mirror.sweep_temps(self.home)
        self.assertEqual(removed, [str(leftover)])
        for kept in (fresh, other_name, elsewhere, user_file):
            self.assertTrue(kept.exists(), kept)

    def test_a_linked_source_folder_is_not_swept(self):
        source = self.mirrored_claude()
        real = temp_dir(self).resolve()
        source.parent.rmdir()
        source.parent.symlink_to(real)
        leftover = self.temp(real, f".{source.name}.77-{'c' * 32}.tmp", 3 * 86400)
        with mock.patch.object(mirror, "_age", side_effect=lambda st, now: now - st.st_mtime):
            self.assertEqual(mirror.sweep_temps(self.home), [])
        self.assertTrue(leftover.exists())

    def test_the_command_sweeps(self):
        self.mirrored_claude()
        result = self.purge("--sweep-tmp", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(decode(result)["data"]["swept"], [])


class DoctorTests(PurgeCase):
    def doctor(self, *args):
        return run_swe(self.home, "doctor", *args)

    def test_doctor_reports_the_mirror(self):
        self.mirrored_claude()
        self.claude(session_id=OTHER_ID)
        self.sync()
        result = self.doctor()
        self.assertIn("Transcript mirror", result.stdout)
        self.assertIn("on", result.stdout)
        self.assertIn("2 copies", result.stdout)
        self.assertIn("1 orphan", result.stdout)
        self.assertIn("last pass", result.stdout)

    def test_doctor_flags_strays_missing_copies_and_stale_temps(self):
        source = self.mirrored_claude()
        write_jsonl(self.mirror_root / "claude/-stray" / f"{OTHER_ID}.jsonl", claude_records())
        other = self.claude(session_id=OTHER_ID)
        self.sync()
        (self.mirror_root / "claude/-work-proj" / other.name).unlink()
        tmp = self.mirror_root / "claude/-work-proj" / f".{source.name}.1-{'a' * 32}.tmp"
        tmp.write_text("x")
        old = time.time() - 3 * 86400
        os.utime(tmp, (old, old))
        with mock.patch.object(mirror, "_age", side_effect=lambda st, now: now - st.st_mtime):
            audit = mirror.audit(self.home)
        self.assertEqual(audit["untracked"], [f"claude/-stray/{OTHER_ID}.jsonl"])
        self.assertEqual(audit["missing"], [f"claude/-work-proj/{other.name}"])
        self.assertEqual(len(audit["stale_temps"]), 1)
        result = self.doctor("--json")
        data = decode(result)["data"]
        checks = {f["check"]: f for f in data["findings"]}
        self.assertIn("mirror.untracked", checks)
        self.assertIn("mirror.missing", checks)
        # Never a failure: the mirror's state cannot fail doctor.
        self.assertFalse(any(f["severity"] == "error" for f in checks.values()
                             if f["check"].startswith("mirror.")))

    def test_bad_mirror_state_never_fails_doctor(self):
        self.mirrored_claude()
        state = json.loads((self.mirror_root / "mirror.json").read_text())
        state["last_pass"].update(failed="lots", files=None, copied=[1], finished_at="soon")
        (self.mirror_root / "mirror.json").write_text(json.dumps(state))
        manifest = json.loads(mirror.manifest_path(self.home).read_text())
        for entry in manifest["files"].values():
            entry["mirror_size"] = "big"
            entry["source"] = 12
        mirror.manifest_path(self.home).write_text(json.dumps(manifest))
        plain = self.doctor()
        self.assertNotIn("Traceback", plain.stderr)
        self.assertIn("Transcript mirror", plain.stdout)
        result = self.doctor("--json")
        self.assertNotIn("Traceback", result.stderr)
        decode(result)

    def test_an_off_mirror_is_said(self):
        mirror.set_enabled(self.home, False)
        data = decode(self.doctor("--json"))["data"]
        self.assertIn("mirror.off", {f["check"] for f in data["findings"]})


class HubChatTests(RestoreCase):
    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patch.start()
        self.addCleanup(patch.stop)

    def session(self, mirrored=True, timestamp=MTIME * 1000.0):
        return Session(timestamp=timestamp, agent="Claude Code", path="/work/proj",
                       session_id=CLAUDE_ID, tool_name="claude", mirrored=mirrored)

    def test_a_mirrored_session_reads_its_chat_from_the_copy(self):
        from swe.remote import tail

        self.mirrored_claude()
        source = tail.make_source(self.session())
        self.assertIsInstance(source, tail.MirrorCopySource)
        texts = [m["text"] for m in source.poll()]
        self.assertEqual(texts, ["fix the login bug", "Merged. CI green."])

    def test_the_copy_is_never_tailed(self):
        from swe.remote import tail

        self.mirrored_claude()
        source = tail.make_source(self.session())
        source.poll()
        copy = self.mirror_root / f"claude/-work-proj/{CLAUDE_ID}.jsonl"
        with open(copy, "a") as fh:
            fh.write(json.dumps(claude_records("late line")[0]) + "\n")
        self.assertEqual(source.poll(), [])

    def test_a_live_session_tails_its_live_file(self):
        from swe.remote import tail

        live = self.claude()
        self.sync()
        source = tail.make_source(self.session(mirrored=False))
        self.assertNotIsInstance(source, tail.MirrorCopySource)
        self.assertEqual(source.path, live)

    def test_other_callers_still_see_only_live_paths(self):
        from swe.remote import tail

        self.mirrored_claude()
        self.assertIsNone(tail.resolve_jsonl_path(self.session()))

    def test_a_restored_session_moves_to_its_live_file(self):
        from swe.remote import tail

        source_path = self.mirrored_claude()
        events = []
        mgr = tail.TailManager(lambda key, ev: events.append(ev), "mbp",
                               home=temp_dir(self))
        mgr.sync([("claude:x", self.session(), "")])
        mgr.tick(time.time())
        self.assertEqual(run_swe(self.home, "session", "restore", CLAUDE_ID).returncode, 0)
        mgr.sync([("claude:x", self.session(mirrored=False), "")])
        tailer = mgr.tailers["claude:x"]
        self.assertEqual(Path(tailer.next_source().path), source_path)
        with open(source_path, "a") as fh:
            fh.write(json.dumps(claude_records("after restore")[0]) + "\n")
        mgr.tick(time.time() + 60)
        texts = [ev["data"].get("text") for ev in events if ev["type"] == "message.appended"]
        self.assertEqual(texts[-1], "after restore")
        self.assertEqual(texts.count("fix the login bug"), 1)


if __name__ == "__main__":
    unittest.main()
