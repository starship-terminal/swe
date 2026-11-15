"""Edge cases of the JSON and JSONL session engines.

The happy paths live in ``test_session_engines.py``. These cover what a
real store throws at the engines: unreadable folders and files, truncated
or wrongly typed records, odd timestamps, adapter callbacks that raise,
and the tail window's boundary.
"""

import json
import os
import stat
import unittest
from unittest.mock import patch
from pathlib import Path

from swe.sessions import failures, parse_cache
from swe.sessions.engines.json_engine import (
    JsonParserConfig,
    _valid_fields,
    parse_json_store,
)
from swe.sessions.engines.jsonl_engine import (
    JsonlParserConfig,
    event_cwd,
    first_user_title,
    parse_jsonl_projects,
)
from tests._tempdir import temp_dir
from tests._settled import settle_cache

_IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


def _write_jsonl(path: Path, records, tail: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [r if isinstance(r, str) else json.dumps(r) for r in records]
    path.write_text("\n".join(lines) + "\n" + tail)
    return path


def _cwd(event: dict) -> str:
    return event.get("cwd") or ""


def _user(event: dict) -> str:
    return event.get("content") or "" if event.get("role") == "user" else ""


class _FailuresClean(unittest.TestCase):
    def setUp(self):
        failures.clear()
        self.addCleanup(failures.clear)
        self.tmp = temp_dir(self)
        self.base = self.tmp / "base"

    def lock(self, path: Path) -> None:
        """Make ``path`` unreadable until the test ends."""
        mode = path.stat().st_mode
        path.chmod(0)
        self.addCleanup(path.chmod, stat.S_IMODE(mode))


class JsonlNestedEdgesTest(_FailuresClean):
    def _config(self, **overrides):
        kwargs = dict(tool_name="demo", agent="Demo", base_dir=str(self.base),
                      path_from_event=_cwd, title_from_event=_user)
        kwargs.update(overrides)
        return JsonlParserConfig(**kwargs)

    def test_project_filter_and_stray_files_in_base(self):
        _write_jsonl(self.base / "keep" / "a.jsonl", [{"cwd": "/k"}])
        _write_jsonl(self.base / "drop" / "b.jsonl", [{"cwd": "/d"}])
        (self.base / "notes.jsonl").write_text('{"cwd": "/stray"}\n')
        sessions = parse_jsonl_projects(self._config(
            project_filter=lambda name, _path: name != "drop"))
        self.assertEqual([(s.session_id, s.path) for s in sessions], [("a", "/k")])

    def test_project_dir_fallback_that_raises_is_treated_as_missing(self):
        _write_jsonl(self.base / "p" / "a.jsonl", [{"role": "user", "content": "q"}])

        def boom(_name):
            raise ValueError("undecodable dir name")

        self.assertEqual(parse_jsonl_projects(self._config(path_from_project_dir=boom)), [])
        sessions = parse_jsonl_projects(self._config(
            path_from_project_dir=boom, default_path="/default"))
        self.assertEqual([(s.path, s.title) for s in sessions], [("/default", "q")])

    def test_one_session_per_dir_takes_the_newest_file(self):
        old = _write_jsonl(self.base / "p" / "old.jsonl", [{"cwd": "/old"}])
        new = _write_jsonl(self.base / "p" / "new.jsonl", [{"cwd": "/new"}])
        os.utime(old, (1_700_000_000, 1_700_000_000))
        os.utime(new, (1_700_000_100, 1_700_000_100))
        (self.base / "empty").mkdir()
        sessions = parse_jsonl_projects(self._config(one_session_per_file=False))
        self.assertEqual([(s.session_id, s.path) for s in sessions], [("new", "/new")])
        self.assertEqual(sessions[0].timestamp, 1_700_000_100_000)

    def test_invalid_utf8_in_the_head_costs_only_that_line(self):
        # A text-mode read raised UnicodeDecodeError on the bad byte, losing
        # the path and title on every later line, and the empty scan was
        # what got cached.
        fp = self.base / "p" / "a.jsonl"
        fp.parent.mkdir(parents=True)
        fp.write_bytes(b'{"note": "bad \xff byte"}\n'
                       + json.dumps({"cwd": "/w"}).encode() + b"\n"
                       + json.dumps({"role": "user", "content": "q"}).encode() + b"\n")
        with parse_cache.activate(self.tmp):
            sessions = parse_jsonl_projects(self._config())
        self.assertEqual([(s.path, s.title) for s in sessions], [("/w", "q")])

    def test_base_that_is_a_file_is_recorded_as_a_failure(self):
        self.base.write_text("not a folder")
        self.assertEqual(parse_jsonl_projects(self._config()), [])
        self.assertIn("demo", failures.snapshot())

    def test_dangling_link_and_zero_mtime_files_are_not_sessions(self):
        proj = self.base / "p"
        _write_jsonl(proj / "good.jsonl", [{"cwd": "/g"}])
        os.symlink(proj / "gone.txt", proj / "dangling.jsonl")
        epoch = _write_jsonl(proj / "epoch.jsonl", [{"cwd": "/e"}])
        os.utime(epoch, (0, 0))
        sessions = parse_jsonl_projects(self._config())
        self.assertEqual([s.session_id for s in sessions], ["good"])
        self.assertEqual(failures.snapshot(), {})

    def test_wrong_json_types_blank_lines_and_a_truncated_tail(self):
        _write_jsonl(self.base / "p" / "s.jsonl", [
            "[1, 2]", "42", '"just a string"', "null", "", "   ",
            "{not json",
            {"cwd": "/w"},
            {"role": "user", "content": "<b>Fix</b>   the\n build"},
        ], tail='{"role": "user", "content": "cut off mid-wr')
        sessions = parse_jsonl_projects(self._config())
        self.assertEqual([(s.path, s.title) for s in sessions], [("/w", "Fix the build")])

    def test_empty_file_falls_back_to_the_project_path(self):
        (self.base / "p").mkdir(parents=True)
        (self.base / "p" / "s.jsonl").write_text("")
        sessions = parse_jsonl_projects(self._config(
            path_from_project_dir=lambda name: f"/from/{name}"))
        self.assertEqual([(s.path, s.title) for s in sessions], [("/from/p", "")])

    def test_callbacks_that_raise_on_one_record_retry_on_the_next(self):
        _write_jsonl(self.base / "p" / "s.jsonl", [
            {"kind": "odd"},
            {"cwd": "/w", "role": "user", "content": "second line"},
        ])

        def path_cb(e):
            return e["cwd"]  # KeyError on the first record

        def title_cb(e):
            if "content" not in e:
                raise KeyError("content")
            return e["content"]

        sessions = parse_jsonl_projects(self._config(
            path_from_event=path_cb, title_from_event=title_cb))
        self.assertEqual([(s.path, s.title) for s in sessions], [("/w", "second line")])

    def test_title_scan_can_stop_after_the_path_window(self):
        records = [{"cwd": "/w"}] + [{"n": i} for i in range(5)]
        records.append({"role": "user", "content": "late prompt"})
        _write_jsonl(self.base / "p" / "s.jsonl", records)
        full = parse_jsonl_projects(self._config())
        self.assertEqual(full[0].title, "late prompt")
        short = parse_jsonl_projects(self._config(title_scan_full=False, path_scan_lines=3))
        self.assertEqual([(s.path, s.title) for s in short], [("/w", "")])

    def test_long_title_is_cut_to_title_max_len(self):
        _write_jsonl(self.base / "p" / "s.jsonl", [
            {"cwd": "/w"}, {"role": "user", "content": "x" * 50}])
        sessions = parse_jsonl_projects(self._config(title_max_len=10))
        self.assertEqual(sessions[0].title, "x" * 10 + "...")

    @unittest.skipIf(_IS_ROOT, "root reads files whatever their mode")
    def test_unreadable_file_still_lists_with_its_fallback(self):
        fp = _write_jsonl(self.base / "p" / "s.jsonl", [
            {"cwd": "/w"}, {"role": "user", "content": "hidden"}])
        self.lock(fp)
        sessions = parse_jsonl_projects(self._config(
            path_from_project_dir=lambda name: f"/from/{name}"))
        self.assertEqual([(s.session_id, s.path, s.title) for s in sessions],
                         [("s", "/from/p", "")])
        self.assertEqual(failures.snapshot(), {})

    @unittest.skipIf(_IS_ROOT, "root reads folders whatever their mode")
    def test_unreadable_project_is_skipped_and_the_rest_still_list(self):
        _write_jsonl(self.base / "ok" / "a.jsonl", [{"cwd": "/ok"}])
        _write_jsonl(self.base / "locked" / "b.jsonl", [{"cwd": "/locked"}])
        self.lock(self.base / "locked")
        sessions = parse_jsonl_projects(self._config())
        self.assertEqual([s.path for s in sessions], ["/ok"])
        self.assertEqual(failures.snapshot(), {})


class JsonlGlobEdgesTest(_FailuresClean):
    def _config(self, **overrides):
        kwargs = dict(tool_name="demo", agent="Demo", base_dir=str(self.base),
                      mode="glob", path_from_event=_cwd)
        kwargs.update(overrides)
        return JsonlParserConfig(**kwargs)

    def test_folder_named_like_a_session_is_skipped(self):
        _write_jsonl(self.base / "a" / "real.jsonl", [{"cwd": "/r"}])
        (self.base / "a" / "folder.jsonl").mkdir()
        sessions = parse_jsonl_projects(self._config())
        self.assertEqual([s.session_id for s in sessions], ["real"])

    def test_session_id_callback_crash_is_recorded(self):
        _write_jsonl(self.base / "a.jsonl", [{"cwd": "/r"}])

        def boom(_fp):
            raise RuntimeError("bad id layout")

        self.assertEqual(parse_jsonl_projects(self._config(session_id_from_path=boom)), [])
        self.assertIn("RuntimeError: bad id layout", failures.snapshot()["demo"])


class JsonlSessionDirsEdgesTest(_FailuresClean):
    def _config(self, **overrides):
        kwargs = dict(tool_name="demo", agent="Demo", base_dir=str(self.base),
                      mode="session_dirs", path_from_event=_cwd, require_path=False)
        kwargs.update(overrides)
        return JsonlParserConfig(**kwargs)

    def test_filters_fallbacks_and_layout_gaps(self):
        _write_jsonl(self.base / "keep" / "chats" / "s1" / "c.jsonl", [{"n": 1}])
        _write_jsonl(self.base / "drop" / "chats" / "s2" / "c.jsonl", [{"cwd": "/d"}])
        (self.base / "nochats").mkdir()  # no chats/ subfolder at all
        (self.base / "keep" / "chats" / "stray.jsonl").write_text("{}\n")
        (self.base / "stray.txt").write_text("x")

        def fallback(name):
            if name == "nochats":
                raise ValueError("unexpected")
            return f"/from/{name}"

        sessions = parse_jsonl_projects(self._config(
            chats_subdir="chats",
            project_filter=lambda name, _p: name != "drop",
            path_from_project_dir=fallback,
        ))
        self.assertEqual([(s.session_id, s.path) for s in sessions], [("s1", "/from/keep")])

        def boom(_name):
            raise ValueError("unexpected")

        sessions = parse_jsonl_projects(self._config(
            chats_subdir="chats", path_from_project_dir=boom))
        self.assertEqual(sorted((s.session_id, s.path) for s in sessions),
                         [("s1", ""), ("s2", "/d")])

    def test_enrich_that_clears_the_path_drops_a_required_session(self):
        _write_jsonl(self.base / "p" / "s1" / "c.jsonl", [{"cwd": "/w"}])

        def clear_path(fields, _dir, _proj):
            fields["path"] = ""

        sessions = parse_jsonl_projects(self._config(
            enrich_session=clear_path, require_path=True))
        self.assertEqual(sessions, [])

    @unittest.skipIf(_IS_ROOT, "root reads folders whatever their mode")
    def test_unreadable_project_is_recorded_as_a_failure(self):
        _write_jsonl(self.base / "p" / "s1" / "c.jsonl", [{"cwd": "/w"}])
        self.lock(self.base / "p")
        self.assertEqual(parse_jsonl_projects(self._config()), [])
        self.assertIn("PermissionError", failures.snapshot()["demo"])


class JsonlIndexEdgesTest(_FailuresClean):
    def _config(self, **overrides):
        kwargs = dict(tool_name="demo", agent="Demo", base_dir=str(self.base),
                      mode="index_jsonl")
        kwargs.update(overrides)
        return JsonlParserConfig(**kwargs)

    def _index(self, proj: str, entries) -> Path:
        return _write_jsonl(self.base / proj / "index.jsonl", entries)

    def test_filters_fallbacks_and_stray_entries(self):
        self._index("keep", [{"id": "k1"}])
        self._index("drop", [{"id": "d1", "cwd": "/d"}])
        self._index("raises", [{"id": "r1"}])
        (self.base / "noindex").mkdir()
        (self.base / "stray.txt").write_text("x")

        def fallback(name):
            if name == "raises":
                raise ValueError("unexpected")
            return f"/from/{name}"

        sessions = parse_jsonl_projects(self._config(
            project_filter=lambda name, _p: name != "drop",
            path_from_project_dir=fallback,
        ))
        # "raises" has no path left and require_path drops it.
        self.assertEqual([(s.session_id, s.path) for s in sessions], [("k1", "/from/keep")])

    def test_invalid_utf8_in_the_index_costs_only_that_entry(self):
        idx = self.base / "p" / "index.jsonl"
        idx.parent.mkdir(parents=True)
        idx.write_bytes(b'{"id": "a", "cwd": "/a\xff"}\n{"id": "b", "cwd": "/b"}\n')
        sessions = parse_jsonl_projects(self._config())
        self.assertEqual([(s.session_id, s.path) for s in sessions],
                         [("a", "/a\ufffd"), ("b", "/b")])

    def test_non_string_cwd_is_missing_not_a_path(self):
        """A wrongly typed cwd must not slip past require_path.

        It was truthy, so the fallback was skipped and require_path passed;
        Session then blanked it, listing a session with no folder.
        """
        self._index("p", [{"id": "a", "cwd": 123}, {"id": "b", "cwd": ["/x"]},
                          {"id": "c", "cwd": "/real"}])
        sessions = parse_jsonl_projects(self._config())
        self.assertEqual([(s.session_id, s.path) for s in sessions], [("c", "/real")])
        sessions = parse_jsonl_projects(self._config(
            path_from_project_dir=lambda name: f"/from/{name}"))
        self.assertEqual([(s.session_id, s.path) for s in sessions],
                         [("a", "/from/p"), ("b", "/from/p"), ("c", "/real")])

    def test_odd_timestamps_fall_through_to_the_next_field(self):
        index = self._index("p", [
            {"id": "a", "cwd": "/w", "updated_at": "last tuesday",
             "created_at": "2026-08-01T00:00:00Z"},
            {"id": "b", "cwd": "/w", "updated_at": "1785542400"},
            {"id": "c", "cwd": "/w", "updated_at": "soon", "created_at": None,
             "timestamp": float("nan")},
        ])
        os.utime(index, (1_700_000_000, 1_700_000_000))
        ts = {s.session_id: s.timestamp for s in parse_jsonl_projects(self._config())}
        self.assertEqual(ts, {"a": 1785542400000.0, "b": 1785542400000.0,
                              "c": 1_700_000_000_000})

    def test_side_file_titles_and_newer_mtime(self):
        index = self._index("p", [{"id": "s1", "cwd": "/w"}, {"id": "s2", "cwd": "/w"}])
        os.utime(index, (1_700_000_000, 1_700_000_000))
        side = _write_jsonl(self.base / "p" / "s1.jsonl", [
            "", "{oops", "[]", {"type": "noise"},
            {"role": "user", "content": "first prompt"},
            {"rename": "Renamed later"},
        ])
        os.utime(side, (1_700_000_500, 1_700_000_500))

        def title_cb(e):
            if e.get("type") == "noise":
                raise KeyError("noise")
            return _user(e)

        def tail_cb(e):
            return (2, e["rename"], "rename") if "rename" in e else None

        sessions = {s.session_id: s for s in parse_jsonl_projects(self._config(
            title_from_event=title_cb))}
        self.assertEqual(sessions["s1"].title, "first prompt")
        self.assertEqual(sessions["s1"].timestamp, 1_700_000_500_000)
        self.assertEqual(sessions["s2"].title, "")
        self.assertEqual(sessions["s2"].timestamp, 1_700_000_000_000)

        sessions = {s.session_id: s for s in parse_jsonl_projects(self._config(
            title_from_event=title_cb, tail_title_from_event=tail_cb))}
        self.assertEqual((sessions["s1"].title, sessions["s1"].title_source),
                         ("Renamed later", "rename"))

    def test_session_file_callback_that_raises_keeps_the_index_title(self):
        self._index("p", [{"id": "s1", "cwd": "/w", "title": "From index"}])
        _write_jsonl(self.base / "p" / "s1.jsonl", [{"rename": "Never read"}])

        def boom(_entry, _proj):
            raise RuntimeError("bad layout")

        sessions = parse_jsonl_projects(self._config(
            session_file_from_entry=boom,
            tail_title_from_event=lambda e: (1, e.get("rename", ""), "rename"),
        ))
        self.assertEqual([s.title for s in sessions], ["From index"])

    def test_side_path_that_is_a_folder_reads_no_title(self):
        self._index("p", [{"id": "s1", "cwd": "/w"}])
        (self.base / "p" / "s1.jsonl").mkdir()
        sessions = parse_jsonl_projects(self._config(
            title_from_event=_user,
            tail_title_from_event=lambda e: (1, "x", "auto"),
        ))
        self.assertEqual([(s.session_id, s.title) for s in sessions], [("s1", "")])

    @unittest.skipIf(_IS_ROOT, "root reads files whatever their mode")
    def test_unreadable_index_lists_nothing_for_that_project(self):
        self.lock(self._index("locked", [{"id": "x", "cwd": "/x"}]))
        self._index("ok", [{"id": "y", "cwd": "/y"}])
        sessions = parse_jsonl_projects(self._config())
        self.assertEqual([s.session_id for s in sessions], ["y"])
        self.assertEqual(failures.snapshot(), {})

    def test_base_that_is_a_file_is_recorded_as_a_failure(self):
        self.base.write_text("x")
        self.assertEqual(parse_jsonl_projects(self._config()), [])
        self.assertIn("demo", failures.snapshot())


class JsonlTailWindowTest(_FailuresClean):
    def _title(self, fp: Path, tail_bytes: int) -> tuple[str, str]:
        def tail_cb(e):
            return (2, e["rename"], "rename") if "rename" in e else None

        config = JsonlParserConfig(
            tool_name="demo", agent="Demo", base_dir=str(fp.parent.parent),
            default_path="/w", tail_title_from_event=tail_cb, tail_bytes=tail_bytes)
        sessions = parse_jsonl_projects(config)
        return sessions[0].title, sessions[0].title_source

    def test_window_starting_on_a_line_boundary_keeps_that_line(self):
        """The record the window opens on is whole, not a partial to drop."""
        first = json.dumps({"pad": "x" * 50})
        last = json.dumps({"rename": "Late name"})
        fp = _write_jsonl(self.base / "p" / "s.jsonl", [first, last])
        self.assertEqual(self._title(fp, len(last) + 1), ("Late name", "rename"))

    def test_window_starting_mid_record_drops_the_fragment(self):
        # A damaged line whose tail would parse as {"rename": ...} if the
        # window's first, partial line were not dropped.
        first = 'xx{"rename": "From a fragment"}'
        last = json.dumps({"other": 1})
        fp = _write_jsonl(self.base / "p" / "s.jsonl", [first, last])
        size = fp.stat().st_size
        cut = first.index('{"rename"')
        # Windows opening just before, on and just after the fragment.
        for tail_bytes in (size - cut + 1, size - cut, size - cut - 1):
            with self.subTest(tail_bytes=tail_bytes):
                self.assertEqual(self._title(fp, tail_bytes), ("", ""))
        self.assertEqual(self._title(fp, size), ("", ""))

    def test_non_dict_records_in_the_tail_are_skipped(self):
        fp = _write_jsonl(self.base / "p" / "s.jsonl", [
            {"rename": "Real"}, "[1]", '"text"', "7"])
        self.assertEqual(self._title(fp, 65536), ("Real", "rename"))


class JsonlEventHelpersTest(unittest.TestCase):
    def test_event_cwd(self):
        self.assertEqual(event_cwd({"cwd": "/a"}), "/a")
        self.assertEqual(event_cwd({"payload": {"working_directory": "/p"}}), "/p")
        self.assertEqual(event_cwd({"cwd": 5, "payload": {"cwd": ["/x"]}, "directory": "/d"}),
                         "/d")
        self.assertEqual(event_cwd({"root": "/r", "cwd": "/ignored"}, "root"), "/r")
        self.assertEqual(event_cwd({"payload": "not a dict", "cwd": ""}), "")

    def test_first_user_title_branches(self):
        # Role mismatch and type not listed: nothing.
        self.assertEqual(first_user_title(
            {"type": "assistant", "message": {"role": "assistant", "content": "a"}}), "")
        # Role mismatch but a listed type still reads the content path.
        self.assertEqual(first_user_title(
            {"type": "user", "message": {"role": "system", "content": "typed"}}), "typed")
        # Content path that runs into a non-dict.
        self.assertEqual(first_user_title(
            {"type": "user", "message": {"role": "user"}},
            content_path=("message", "content", "text")), "")
        # No role path: the type alone decides.
        self.assertEqual(first_user_title(
            {"type": "message", "message": "plain"}, role_path=()), "plain")
        self.assertEqual(first_user_title(
            {"type": "message", "content": [{"type": "text", "text": "blk"}]},
            role_path=()), "blk")
        self.assertEqual(first_user_title({"type": "tool"}, role_path=()), "")


class JsonFilesEdgesTest(_FailuresClean):
    def _config(self, **overrides):
        kwargs = dict(tool_name="demo", agent="Demo", mode="files",
                      base_dir=str(self.base))
        kwargs.update(overrides)
        return JsonParserConfig(**kwargs)

    def _write(self, name: str, data) -> Path:
        self.base.mkdir(parents=True, exist_ok=True)
        fp = self.base / name
        fp.write_text(data if isinstance(data, str) else json.dumps(data))
        return fp

    def test_cached_and_uncached_reads_agree(self):
        self._write("a.json", {"id": "a", "cwd": "/a", "title": "Alpha"})
        self._write("b.json", "{truncated")
        self._write("c.json", {"id": "c"})  # no path: dropped
        self._write("skipme.json", {"id": "s", "cwd": "/s"})
        (self.base / "dir.json").mkdir()
        for cache in (False, True):
            with self.subTest(cache=cache):
                sessions = parse_json_store(self._config(
                    cache=cache, skip_basenames={"skipme.json"},
                    include=lambda e, _fp: e.get("id") != "x"))
                self.assertEqual([(s.session_id, s.path, s.title) for s in sessions],
                                 [("a", "/a", "Alpha")])
        self.assertEqual(failures.snapshot(), {})

    def test_cached_read_honours_include(self):
        settle_cache(self)
        self._write("a.json", {"id": "a", "cwd": "/a"})
        self._write("b.json", {"id": "b", "cwd": "/b"})
        config = self._config(cache=True, include=lambda e, _fp: e["id"] == "b")
        with parse_cache.activate(home=self.tmp):
            sessions = parse_json_store(config)
            self.assertEqual([s.session_id for s in sessions], ["b"])
            # Unchanged files come from the cache, the excluded one too.
            with patch("swe.sessions.engines.json_engine._file_fields",
                       side_effect=AssertionError("read again")):
                again = parse_json_store(config)
        self.assertEqual([s.session_id for s in again], ["b"])

    @unittest.skipIf(_IS_ROOT, "root reads files whatever their mode")
    def test_unreadable_file_is_skipped(self):
        locked = self._write("a.json", {"id": "a", "cwd": "/a"})
        mode = stat.S_IMODE(locked.stat().st_mode)
        self.lock(locked)
        self._write("b.json", {"id": "b", "cwd": "/b"})
        sessions = parse_json_store(self._config(cache=False))
        self.assertEqual([s.session_id for s in sessions], ["b"])
        locked.chmod(mode)
        # A read that fails without touching the file (so its stat, and the
        # cache key, stay the same) must not be pinned as "no session".
        real_open = open

        def flaky_open(path, *args, **kwargs):
            if str(path) == str(locked):
                raise PermissionError(13, "Permission denied", str(path))
            return real_open(path, *args, **kwargs)

        with parse_cache.activate(home=self.tmp):
            with patch("builtins.open", flaky_open):
                sessions = parse_json_store(self._config(cache=True))
            self.assertEqual([s.session_id for s in sessions], ["b"])
            sessions = parse_json_store(self._config(cache=True))
        self.assertEqual(sorted(s.session_id for s in sessions), ["a", "b"])

    def test_include_that_raises_is_recorded(self):
        self._write("a.json", {"id": "a", "cwd": "/a"})

        def boom(_e, _fp):
            raise TypeError("include broke")

        self.assertEqual(parse_json_store(self._config(include=boom)), [])
        self.assertIn("TypeError: include broke", failures.snapshot()["demo"])

    def test_session_id_falls_back_to_the_file_name(self):
        self._write("abc.jsonl", {"cwd": "/a"})
        sessions = parse_json_store(self._config(file_glob="*.jsonl"))
        self.assertEqual([s.session_id for s in sessions], ["abc"])

    def test_valid_fields_rejects_a_damaged_cache_entry(self):
        good = {"timestamp": 1.0, "agent": "A", "path": "/p", "title": "",
                "session_id": "s", "tool_name": "t", "title_source": ""}
        self.assertTrue(_valid_fields(None))
        self.assertTrue(_valid_fields(good))
        self.assertFalse(_valid_fields({k: v for k, v in good.items() if k != "path"}))
        self.assertFalse(_valid_fields({**good, "extra": 1}))
        self.assertFalse(_valid_fields(["not", "a", "dict"]))


class JsonToSessionEdgesTest(_FailuresClean):
    def _index(self, data, **overrides) -> list:
        self.tmp.joinpath("index.json").write_text(json.dumps(data))
        kwargs = dict(tool_name="demo", agent="Demo", index_path=str(self.tmp / "index.json"))
        kwargs.update(overrides)
        return parse_json_store(JsonParserConfig(**kwargs))

    def test_non_string_cwd_is_missing_not_a_path(self):
        """A wrongly typed cwd must not slip past require_path or default_path."""
        entries = [{"id": "a", "cwd": 123}, {"id": "b", "cwd": {"p": 1}},
                   {"id": "c", "cwd": "file:///Users/me/My%20Proj"}]
        self.assertEqual([(s.session_id, s.path) for s in self._index(entries)],
                         [("c", "/Users/me/My Proj")])
        self.assertEqual([(s.session_id, s.path) for s in self._index(
            entries, default_path="/d")],
            [("a", "/d"), ("b", "/d"), ("c", "/Users/me/My Proj")])

    def test_non_dict_entries_use_the_file_and_the_defaults(self):
        index = self.tmp / "index.json"
        sessions = self._index(["just a string"], require_path=False)
        self.assertEqual([(s.session_id, s.path, s.title) for s in sessions],
                         [("index", "", "")])
        self.assertEqual(sessions[0].timestamp, os.path.getmtime(index) * 1000)

    def test_scalar_index_and_named_lists(self):
        self.assertEqual(self._index(42), [])
        for key in ("sessions", "items", "threads"):
            with self.subTest(key=key):
                sessions = self._index({key: [{"id": key, "cwd": "/w"}]})
                self.assertEqual([s.session_id for s in sessions], [key])
        self.assertEqual(failures.snapshot(), {})

    def test_include_filters_index_entries(self):
        sessions = self._index([{"id": "a", "cwd": "/w"}, {"id": "b", "cwd": "/w"}],
                               include=lambda e, _fp: e["id"] == "b")
        self.assertEqual([s.session_id for s in sessions], ["b"])

    def test_odd_timestamps(self):
        index = self.tmp / "index.json"
        sessions = self._index([
            {"id": "a", "cwd": "/w", "updatedAt": "garbage", "dateCreated": 1785542400},
            {"id": "b", "cwd": "/w", "updated_at": "", "last_updated": "2026-08-01T00:00:00Z"},
            {"id": "c", "cwd": "/w", "updated_at": float("inf")},
        ])
        mtime = os.path.getmtime(index) * 1000
        self.assertEqual({s.session_id: s.timestamp for s in sessions},
                         {"a": 1785542400000.0, "b": 1785542400000.0, "c": mtime})
        sessions = self._index([{"id": "d", "cwd": "/w"}], get_ts=lambda e, fp: "soon")
        self.assertEqual(sessions[0].timestamp, os.path.getmtime(index) * 1000)

    def test_enrich_that_raises_keeps_fields_and_clearing_path_drops(self):
        def half_then_boom(fields, _e, _fp):
            fields["title"] = "set before the crash"
            raise RuntimeError("side file missing")

        sessions = self._index([{"id": "a", "cwd": "/w"}], enrich=half_then_boom)
        self.assertEqual([(s.path, s.title) for s in sessions], [("/w", "set before the crash")])

        def clear(fields, _e, _fp):
            fields["path"] = ""

        self.assertEqual(self._index([{"id": "a", "cwd": "/w"}], enrich=clear), [])


class JsonNestedDirsEdgesTest(_FailuresClean):
    def _config(self, **overrides):
        kwargs = dict(tool_name="demo", agent="Demo", mode="nested_dirs",
                      base_dir=str(self.base))
        kwargs.update(overrides)
        return JsonParserConfig(**kwargs)

    def _seed(self, parent: str, sid: str, summary=None) -> Path:
        d = self.base / parent / sid
        d.mkdir(parents=True)
        if summary is not None:
            (d / "summary.json").write_text(json.dumps(summary))
        return d

    def test_filters_overrides_and_fallbacks(self):
        self._seed("proj", "s1", {"cwd": "/own", "alt_id": "custom"})
        self._seed("proj", "s2", {"title": "No cwd"})
        self._seed("proj", "s3", {"skip": True})
        (self.base / "proj" / "loose.txt").write_text("x")
        (self.base / "stray.txt").write_text("x")

        def get_id(e, _fp):
            if e.get("title"):
                raise KeyError("alt_id")
            return e.get("alt_id")

        def get_path(e, _fp):
            if "title" in e:
                raise KeyError("cwd")
            return e.get("cwd")

        config = self._config(
            path_from_parent=lambda name: f"/from/{name}",
            include=lambda e, _fp: not e.get("skip"),
            get_id=get_id, get_path=get_path,
        )
        sessions = parse_json_store(config)
        self.assertEqual(sorted((s.session_id, s.path, s.title) for s in sessions),
                         [("custom", "/own", ""), ("s2", "/from/proj", "No cwd")])
        # The wrappers are put back, so a second parse sees the same config.
        self.assertIs(config.get_id, get_id)
        self.assertIs(config.get_path, get_path)

    def test_parent_fallback_that_raises_is_treated_as_missing(self):
        self._seed("proj", "s1", {"title": "t"})

        def boom(_name):
            raise ValueError("undecodable")

        self.assertEqual(parse_json_store(self._config(path_from_parent=boom)), [])

    def test_zero_mtime_summary_takes_the_session_dir_time(self):
        d = self._seed("proj", "s1", {"title": "t"})
        os.utime(d / "summary.json", (0, 0))
        os.utime(d, (1_700_000_000, 1_700_000_000))
        sessions = parse_json_store(self._config(path_from_parent=lambda _n: "/w"))
        self.assertEqual([(s.session_id, s.timestamp) for s in sessions],
                         [("s1", 1_700_000_000_000)])

    @unittest.skipIf(_IS_ROOT, "root reads folders whatever their mode")
    def test_unreadable_parent_is_recorded_as_a_failure(self):
        self._seed("proj", "s1", {"cwd": "/w"})
        self.lock(self.base / "proj")
        self.assertEqual(parse_json_store(self._config()), [])
        self.assertIn("PermissionError", failures.snapshot()["demo"])


class JsonProjectMapEdgesTest(_FailuresClean):
    def _config(self, **overrides):
        kwargs = dict(tool_name="demo", agent="Demo", mode="project_map",
                      index_path=str(self.tmp / "map.json"),
                      session_dir_from_item=lambda key, meta: str(self.base / str(meta)))
        kwargs.update(overrides)
        return JsonParserConfig(**kwargs)

    def _map(self, data) -> None:
        (self.tmp / "map.json").write_text(data if isinstance(data, str) else json.dumps(data))

    def test_items_dirs_and_primary_shapes(self):
        self._map({"projects": {"/w/a": "a", "/w/b": "b", "/w/c": "c", "/w/gone": "gone",
                                "/w/empty": "", "/w/boom": "boom", "/w/skip": "skip"}})
        for name in ("a", "b", "c", "skip"):
            (self.base / name).mkdir(parents=True)
        (self.base / "a" / "summary.json").write_text(json.dumps(["list", "payload"]))
        # A dict primary merges into the entry; the map key stays the path.
        (self.base / "c" / "summary.json").write_text(
            json.dumps({"title": "Merged", "path": "/from/file"}))

        def sess_dir(key, meta):
            if meta == "boom":
                raise RuntimeError("bad meta")
            return str(self.base / meta) if meta else ""

        sessions = parse_json_store(self._config(
            session_dir_from_item=sess_dir,
            include=lambda e, _fp: e["meta"] != "skip",
            get_title=lambda e, _fp: e.get("title") or (e.get("data") or ["no data"])[0],
        ))
        self.assertEqual(sorted((s.path, s.title) for s in sessions),
                         [("/w/a", "list"), ("/w/b", "no data"), ("/w/c", "Merged")])
        self.assertEqual(failures.snapshot(), {})

    def test_map_items_callback_and_get_path_override(self):
        self._map({"rows": [["/w/a", "a"], ["/w/b", "b"], ["bad"]]})
        for name in ("a", "b"):
            (self.base / name).mkdir(parents=True)

        def get_path(e, _fp):
            if e["meta"] == "b":
                raise KeyError("cwd")
            return "/override/" + e["meta"]

        sessions = parse_json_store(self._config(
            session_file="",
            map_items=lambda data: [tuple(r) for r in data["rows"]],
            get_path=get_path,
        ))
        self.assertEqual(sorted(s.path for s in sessions), ["/override/a", "/w/b"])

    def test_list_index_has_no_pairs_to_map(self):
        self._map([{"path": "/w/a"}])
        self.assertEqual(parse_json_store(self._config()), [])

    def test_malformed_map_is_recorded_as_a_failure(self):
        self._map("{not json")
        self.assertEqual(parse_json_store(self._config()), [])
        self.assertIn("JSONDecodeError", failures.snapshot()["demo"])


if __name__ == "__main__":
    unittest.main()
