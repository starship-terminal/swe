"""Regenerate python-index.json: the search rows swe's own writer leaves for
the mirror scenario `SearchMirrorCrossWriterTests` replays in Swift.

Run from the repository root:

    python3 app/mac/Tests/SWECoreTests/Fixtures/mirror/generate_python_index.py

It seeds a throwaway HOME with the transcripts in this folder, runs the
real `swe session mirror` and `swe session index` from source
(`PYTHONPATH=src`), prunes sources between the passes exactly as the Swift
test does, and dumps the index with paths relative to HOME. Nothing outside
the temporary HOME is read or written.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[5]
MTIME = 1_790_000_000
A = "11111111-2222-3333-4444-555555555555"
B = "22222222-2222-3333-4444-555555555555"
C = "33333333-2222-3333-4444-555555555555"
STEM = "rollout-2026-09-01T10-00-00-0199aaaa-bbbb-cccc-dddd-eeeeffff0001"


def place(home: Path, name: str, rel: str, mtime: int, append: bool = False) -> Path:
    target = home / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "ab" if append else "wb") as handle:
        handle.write((HERE / name).read_bytes())
    os.utime(target, (mtime, mtime))
    return target


def swe(home: Path, *args: str) -> None:
    env = {**os.environ, "HOME": str(home), "PYTHONPATH": str(REPO / "src")}
    subprocess.run([sys.executable, "-m", "swe.cli", "session", *args, "--json"], env=env,
                   check=True, stdout=subprocess.DEVNULL, cwd=home)


def dump(home: Path) -> dict:
    prefixes = sorted({str(home), os.path.realpath(home)}, key=len, reverse=True)

    def rel(path):
        for prefix in prefixes:
            if path and path.startswith(prefix + "/"):
                return path[len(prefix) + 1:]
        return path

    conn = sqlite3.connect(home / ".swe/data/search/search.sqlite")
    files = []
    for row in conn.execute(
            "SELECT f.path, f.byte_offset, f.mtime_ms, f.size_bytes, f.state, s.id, s.agent,"
            " s.session_id, s.file_path, s.title, s.cwd, s.created_at, s.updated_at,"
            " s.message_count FROM files f LEFT JOIN sessions s ON s.id = f.session_row_id"):
        mark = conn.execute("SELECT value FROM meta WHERE key = ?",
                            ("mirror_copy:" + row[0],)).fetchone()
        files.append({
            "path": rel(row[0]), "byte_offset": row[1], "mtime_ms": row[2], "size_bytes": row[3],
            "state": row[4], "agent": row[6], "session_id": row[7], "file_path": rel(row[8]),
            "title": row[9], "cwd": row[10], "created_at": row[11], "updated_at": row[12],
            "message_count": row[13],
            # The mark's agent, and whether it names this row's session.
            "mark": None if mark is None else {
                "agent": mark[0].split(" ")[0], "names_session": mark[0].split(" ")[1] == str(row[5])},
        })
    messages = [{"file_path": rel(r[0]), "role": r[1], "ts": r[2], "user_text": r[3],
                 "assistant_text": r[4], "tool_text": r[5], "identifiers": r[6]}
                for r in conn.execute(
                    "SELECT s.file_path, m.role, m.ts, f.user_text, f.assistant_text, f.tool_text,"
                    " f.identifiers FROM messages m JOIN sessions s ON s.id = m.session_row_id"
                    " JOIN messages_fts f ON f.rowid = m.id ORDER BY s.file_path, m.id")]
    sessions = conn.execute("SELECT count(*) FROM sessions").fetchone()[0]
    conn.close()
    return {"files": sorted(files, key=lambda f: f["path"]), "messages": messages,
            "sessions": sessions}


def main() -> None:
    # With a folder argument the home is built there and kept, so the Swift
    # writer can be run over the index swe wrote
    # (`SearchMirrorCrossWriterTests.scanAFolderSweIndexed`).
    keep = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    home = keep or Path(tempfile.mkdtemp(prefix="swe-mirror-fixture-"))
    try:
        pruned = place(home, "claude-pruned.jsonl", f".claude/projects/-work-proj/{A}.jsonl", MTIME)
        place(home, "claude-live.jsonl", f".claude/projects/-work-proj/{B}.jsonl", MTIME)
        grown = place(home, "claude-grown.jsonl", f".claude/projects/-work-other/{C}.jsonl", MTIME)
        codex = place(home, "codex-pruned.jsonl", f".codex/sessions/2026/09/01/{STEM}.jsonl", MTIME)
        # 1. Mirror everything, prune two sessions the index never read.
        swe(home, "mirror")
        pruned.unlink()
        codex.unlink()
        # 2. Index: two live files, two sessions from their copies.
        swe(home, "index")
        # 3. One indexed session grows, is mirrored again, then pruned.
        place(home, "claude-grown-more.jsonl", f".claude/projects/-work-other/{C}.jsonl", MTIME + 100,
              append=True)
        swe(home, "mirror")
        grown.unlink()
        # 4. Index again: the grown copy replaces the stale row in place.
        swe(home, "index")
        out = (keep / "python-index.json") if keep else HERE / "python-index.json"
        out.write_text(json.dumps(dump(home), indent=1, sort_keys=True) + "\n")
        print(f"wrote {out}")
    finally:
        if keep is None:
            shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    main()
